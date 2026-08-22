#!/usr/bin/env python3
"""Publish a verified runtime archive to an S3-compatible object store.

The publisher is deliberately provider-neutral.  It publishes an immutable
archive and its manifest under content-addressed keys, then updates one small
channel object as the final operation.  A missing object-store configuration
is a successful no-op; a partially configured one fails closed.  No provider
credentials are ever included in logs or output.

The archive is uploaded with a sequential, bounded multipart transfer.  The
default part size is 128 MiB and the only other supported size is 256 MiB.
The implementation does not copy large objects, delete old versions, or
perform lifecycle/retention management.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Protocol, Sequence, cast
from urllib.parse import urlparse

try:
    import boto3
    from botocore.client import Config as BotoConfig
    from botocore.exceptions import ClientError
except ImportError:  # pragma: no cover - exercised by the CLI error path
    boto3 = None  # type: ignore[assignment]
    BotoConfig = None  # type: ignore[assignment,misc]
    ClientError = None  # type: ignore[assignment,misc]

from runtime_manifest import RuntimeManifest, RuntimeManifestError, validate_manifest


MIB = 1024 * 1024
DEFAULT_PART_SIZE_BYTES = 128 * MIB
ALLOWED_PART_SIZE_MIB = (128, 256)
MAX_MULTIPART_PARTS = 10_000
MAX_PREFIX_LENGTH = 512
MAX_CHANNEL_LENGTH = 128
MAX_RUNTIME_MANIFEST_BYTES = 128 * MIB
MAX_CHANNEL_MANIFEST_BYTES = 64 * 1024
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
CHANNEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class RuntimePublisherError(RuntimeError):
    """Raised when a runtime cannot be safely published."""


class ObjectStoreClient(Protocol):
    """Small subset of the boto3 S3 client used by this publisher."""

    def head_object(self, *, Bucket: str, Key: str) -> Mapping[str, object]: ...

    def create_multipart_upload(
        self,
        *,
        Bucket: str,
        Key: str,
        ContentType: str,
        Metadata: Mapping[str, str],
        CacheControl: str,
    ) -> Mapping[str, object]: ...

    def upload_part(
        self,
        *,
        Bucket: str,
        Key: str,
        UploadId: str,
        PartNumber: int,
        Body: bytes,
    ) -> Mapping[str, object]: ...

    def complete_multipart_upload(
        self,
        *,
        Bucket: str,
        Key: str,
        UploadId: str,
        MultipartUpload: Mapping[str, object],
    ) -> Mapping[str, object]: ...

    def abort_multipart_upload(self, *, Bucket: str, Key: str, UploadId: str) -> Mapping[str, object]: ...

    def put_object(
        self,
        *,
        Bucket: str,
        Key: str,
        Body: bytes,
        ContentType: str,
        Metadata: Mapping[str, str],
        CacheControl: str,
    ) -> Mapping[str, object]: ...


@dataclass(frozen=True)
class StoreConfig:
    """S3-compatible endpoint configuration.

    ``region`` defaults to ``auto`` because that is accepted by Cloudflare's
    S3-compatible endpoint.  AWS-compatible deployments should set an AWS
    region explicitly.
    """

    endpoint: str
    bucket: str
    access_key_id: str
    secret_access_key: str
    region: str = "auto"
    prefix: str = ""

    def validate(self) -> None:
        parsed = urlparse(self.endpoint)
        if parsed.scheme != "https" or not parsed.netloc:
            raise RuntimePublisherError("object-store endpoint must be an HTTPS URL")
        if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
            raise RuntimePublisherError("object-store endpoint must not contain credentials or query data")
        if any(char.isspace() or ord(char) < 32 for char in self.bucket):
            raise RuntimePublisherError("object-store bucket contains unsafe characters")
        if not self.bucket or len(self.bucket) > 255:
            raise RuntimePublisherError("object-store bucket is empty or too long")
        if not self.access_key_id or not self.secret_access_key:
            raise RuntimePublisherError("object-store credentials are incomplete")
        for credential in (self.access_key_id, self.secret_access_key):
            if len(credential) > 4096 or any(ord(char) < 32 for char in credential):
                raise RuntimePublisherError("object-store credentials contain unsafe characters")
        if not self.region or len(self.region) > 128 or any(ord(char) < 32 for char in self.region):
            raise RuntimePublisherError("object-store region is invalid")
        _validate_prefix(self.prefix)


@dataclass(frozen=True)
class PublishInput:
    archive_path: Path
    manifest_path: Path
    manifest_bytes: bytes
    manifest: RuntimeManifest
    archive_size_bytes: int
    archive_sha256: str
    archive_key: str
    manifest_key: str
    channel_key: str
    channel: str
    manifest_sha256: str


@dataclass(frozen=True)
class PublishResult:
    status: str
    channel: str
    archive_key: str
    manifest_key: str
    channel_key: str
    archive_size_bytes: int
    archive_sha256: str
    manifest_sha256: str
    archive_uploaded: bool = False
    manifest_uploaded: bool = False
    channel_updated: bool = False

    def as_json(self) -> dict[str, object]:
        return {
            "status": self.status,
            "channel": self.channel,
            "archive_key": self.archive_key,
            "manifest_key": self.manifest_key,
            "channel_key": self.channel_key,
            "archive_size_bytes": self.archive_size_bytes,
            "archive_sha256": self.archive_sha256,
            "manifest_sha256": self.manifest_sha256,
            "archive_uploaded": self.archive_uploaded,
            "manifest_uploaded": self.manifest_uploaded,
            "channel_updated": self.channel_updated,
        }


def _env_value(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def _validate_prefix(prefix: str) -> str:
    if len(prefix) > MAX_PREFIX_LENGTH or "\\" in prefix or "\x00" in prefix:
        raise RuntimePublisherError("object-store prefix is unsafe")
    normalized = prefix.strip("/")
    if not normalized:
        return ""
    parts = normalized.split("/")
    if any(not part or part in (".", "..") for part in parts):
        raise RuntimePublisherError("object-store prefix contains an unsafe path component")
    if any(any(ord(char) < 32 for char in part) for part in parts):
        raise RuntimePublisherError("object-store prefix contains control characters")
    return "/".join(parts)


def _validate_channel(channel: str) -> str:
    if not CHANNEL_RE.fullmatch(channel) or channel in (".", ".."):
        raise RuntimePublisherError("channel must be a single safe path component")
    return channel


def _join_key(prefix: str, *parts: str) -> str:
    checked_prefix = _validate_prefix(prefix)
    checked_parts: list[str] = []
    for part in parts:
        if not part or "/" in part or "\\" in part or "\x00" in part or part in (".", ".."):
            raise RuntimePublisherError("generated object key contains an unsafe component")
        checked_parts.append(part)
    components = ([checked_prefix] if checked_prefix else []) + checked_parts
    key = "/".join(components)
    if not key or len(key) > 2048:
        raise RuntimePublisherError("generated object key is empty or too long")
    return key


def _sha256_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(8 * MIB)
                if not chunk:
                    break
                size += len(chunk)
                digest.update(chunk)
    except OSError as error:
        raise RuntimePublisherError(f"cannot read archive: {error}") from error
    return size, digest.hexdigest()


def _read_manifest(path: Path) -> tuple[bytes, RuntimeManifest]:
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise RuntimePublisherError(f"cannot read runtime manifest: {error}") from error
    if not payload or len(payload) > MAX_RUNTIME_MANIFEST_BYTES:
        raise RuntimePublisherError("runtime manifest is empty or exceeds the bounded size limit")
    try:
        value = json.loads(payload.decode("utf-8"))
        manifest = validate_manifest(value)
    except (UnicodeDecodeError, json.JSONDecodeError, RuntimeManifestError) as error:
        raise RuntimePublisherError(f"runtime manifest is invalid: {error}") from error
    return payload, manifest


def _manifest_name(manifest_sha256: str) -> str:
    """Return a key component addressed by the complete manifest bytes.

    Runtime and archive digests intentionally exclude launcher compatibility
    metadata.  Addressing a manifest by only those two digests would therefore
    collide when the same runtime archive is published for a new immutable
    launcher.  The manifest's own digest covers that compatibility contract as
    well as its source metadata and file tree.
    """

    if not SHA256_RE.fullmatch(manifest_sha256):
        raise RuntimePublisherError("runtime manifest contains an invalid digest")
    return f"sha256-{manifest_sha256}.json"


def prepare_publish(
    archive_path: Path,
    manifest_path: Path,
    *,
    prefix: str,
    channel: str,
) -> PublishInput:
    """Validate local inputs and derive all immutable/mutable object keys."""

    if not archive_path.is_file():
        raise RuntimePublisherError("runtime archive does not exist")
    if not manifest_path.is_file():
        raise RuntimePublisherError("runtime manifest does not exist")
    manifest_bytes, manifest = _read_manifest(manifest_path)
    archive = manifest["archive"]
    expected_name = archive["object_name"]
    if archive_path.name != expected_name:
        raise RuntimePublisherError("archive filename does not match runtime manifest")
    expected_size = archive["size_bytes"]
    expected_sha256 = archive["sha256"]
    size, digest = _sha256_file(archive_path)
    if size != expected_size or digest != expected_sha256:
        raise RuntimePublisherError("runtime archive digest or size does not match manifest")

    safe_channel = _validate_channel(channel)
    safe_prefix = _validate_prefix(prefix)
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    archive_key = _join_key(safe_prefix, "runtimes", "archives", expected_name)
    manifest_key = _join_key(safe_prefix, "runtimes", "manifests", _manifest_name(manifest_sha256))
    channel_key = _join_key(safe_prefix, "runtimes", "channels", f"{safe_channel}.json")
    return PublishInput(
        archive_path=archive_path,
        manifest_path=manifest_path,
        manifest_bytes=manifest_bytes,
        manifest=manifest,
        archive_size_bytes=size,
        archive_sha256=digest,
        archive_key=archive_key,
        manifest_key=manifest_key,
        channel_key=channel_key,
        channel=safe_channel,
        manifest_sha256=manifest_sha256,
    )


def build_channel_manifest(item: PublishInput) -> bytes:
    """Build the small immutable-reference channel payload.

    The channel object contains keys, not URLs or credentials.  It is written
    as the final operation, so readers see either the old complete pointer or
    the new complete pointer, never a partially written JSON object.
    """

    value: dict[str, object] = {
        "schema_version": 1,
        "channel": item.channel,
        "runtime_digest": item.manifest["runtime_digest"],
        "archive_key": item.archive_key,
        "archive_sha256": item.archive_sha256,
        "archive_size_bytes": item.archive_size_bytes,
        "manifest_key": item.manifest_key,
        "manifest_sha256": item.manifest_sha256,
        # Keep the channel bounded even when the complete manifest is large
        # (the production file tree is tens of MiB). Edge control-plane
        # readers use these publisher-verified scalar facts for admission;
        # the CPU materializer still downloads and verifies the complete
        # manifest before it ever publishes READY.json on a Network Volume.
        "manifest_size_bytes": len(item.manifest_bytes),
        "expanded_bytes": item.manifest["file_tree"]["total_bytes"],
    }
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(payload) > MAX_CHANNEL_MANIFEST_BYTES:
        raise RuntimePublisherError("channel manifest exceeds the bounded size limit")
    return payload


def _is_not_found(error: BaseException) -> bool:
    if ClientError is not None and isinstance(error, ClientError):
        response = getattr(error, "response", {})
        if isinstance(response, Mapping):
            raw_error = response.get("Error")
            if isinstance(raw_error, Mapping):
                code = raw_error.get("Code")
                return code in ("404", "NoSuchKey", "NotFound")
    return False


class RuntimePublisher:
    """Fail-closed object publisher with bounded sequential multipart I/O."""

    def __init__(self, client: ObjectStoreClient, config: StoreConfig, *, part_size_bytes: int = DEFAULT_PART_SIZE_BYTES) -> None:
        config.validate()
        if part_size_bytes < 5 * MIB or part_size_bytes > 256 * MIB:
            raise RuntimePublisherError("multipart part size is outside the bounded 5-256 MiB range")
        self.client = client
        self.config = config
        self.part_size_bytes = part_size_bytes

    def _head(self, key: str) -> Mapping[str, object] | None:
        try:
            return self.client.head_object(Bucket=self.config.bucket, Key=key)
        except Exception as error:
            if _is_not_found(error):
                return None
            raise RuntimePublisherError("object-store HEAD failed") from error

    @staticmethod
    def _head_matches(response: Mapping[str, object], *, expected_size: int, expected_sha256: str) -> bool:
        size = response.get("ContentLength")
        metadata = response.get("Metadata")
        observed_sha256: object = None
        if isinstance(metadata, Mapping):
            observed_sha256 = metadata.get("sha256")
            if observed_sha256 is None:
                observed_sha256 = metadata.get("x-amz-meta-sha256")
        return size == expected_size and observed_sha256 == expected_sha256

    def _immutable_exists(self, key: str, *, expected_size: int, expected_sha256: str) -> bool:
        response = self._head(key)
        if response is None:
            return False
        if not self._head_matches(response, expected_size=expected_size, expected_sha256=expected_sha256):
            raise RuntimePublisherError("existing content-addressed object failed size or SHA-256 verification")
        return True

    def _abort(self, key: str, upload_id: str) -> None:
        try:
            self.client.abort_multipart_upload(Bucket=self.config.bucket, Key=key, UploadId=upload_id)
        except Exception as error:
            if not _is_not_found(error):
                print("runtime publisher: multipart abort failed; provider cleanup may be required", file=sys.stderr)

    def _multipart_upload(self, item: PublishInput) -> None:
        try:
            response = self.client.create_multipart_upload(
                Bucket=self.config.bucket,
                Key=item.archive_key,
                ContentType="application/zstd",
                Metadata={"sha256": item.archive_sha256, "runtime-digest": item.manifest["runtime_digest"]},
                CacheControl="public,max-age=31536000,immutable",
            )
        except Exception as error:
            raise RuntimePublisherError("object-store multipart initialization failed") from error
        upload_id = response.get("UploadId")
        if not isinstance(upload_id, str) or not upload_id:
            raise RuntimePublisherError("object-store multipart initialization returned no upload ID")

        parts: list[dict[str, object]] = []
        digest = hashlib.sha256()
        size = 0
        try:
            with item.archive_path.open("rb") as handle:
                part_number = 1
                while True:
                    body = handle.read(self.part_size_bytes)
                    if not body:
                        break
                    if part_number > MAX_MULTIPART_PARTS:
                        raise RuntimePublisherError("runtime archive exceeds the multipart part limit")
                    digest.update(body)
                    size += len(body)
                    try:
                        part_response = self.client.upload_part(
                            Bucket=self.config.bucket,
                            Key=item.archive_key,
                            UploadId=upload_id,
                            PartNumber=part_number,
                            Body=body,
                        )
                    except Exception as error:
                        raise RuntimePublisherError("object-store multipart part upload failed") from error
                    etag = part_response.get("ETag")
                    if not isinstance(etag, str) or not etag:
                        raise RuntimePublisherError("object-store multipart part returned no ETag")
                    parts.append({"ETag": etag, "PartNumber": part_number})
                    part_number += 1
            if size != item.archive_size_bytes or digest.hexdigest() != item.archive_sha256:
                raise RuntimePublisherError("runtime archive changed while uploading")
            if not parts:
                raise RuntimePublisherError("runtime archive is empty")
            try:
                self.client.complete_multipart_upload(
                    Bucket=self.config.bucket,
                    Key=item.archive_key,
                    UploadId=upload_id,
                    MultipartUpload={"Parts": parts},
                )
            except Exception as error:
                raise RuntimePublisherError("object-store multipart completion failed") from error
        except Exception:
            self._abort(item.archive_key, upload_id)
            raise

        response = self._head(item.archive_key)
        if response is None or not self._head_matches(
            response,
            expected_size=item.archive_size_bytes,
            expected_sha256=item.archive_sha256,
        ):
            raise RuntimePublisherError("completed archive failed post-upload verification")

    def _put_small(self, *, key: str, payload: bytes, content_type: str, immutable: bool) -> bool:
        digest = hashlib.sha256(payload).hexdigest()
        if immutable and self._immutable_exists(key, expected_size=len(payload), expected_sha256=digest):
            return False
        try:
            self.client.put_object(
                Bucket=self.config.bucket,
                Key=key,
                Body=payload,
                ContentType=content_type,
                Metadata={"sha256": digest},
                CacheControl="public,max-age=31536000,immutable" if immutable else "no-cache",
            )
        except Exception as error:
            raise RuntimePublisherError("object-store object upload failed") from error
        response = self._head(key)
        if response is None or not self._head_matches(
            response,
            expected_size=len(payload),
            expected_sha256=digest,
        ):
            raise RuntimePublisherError("uploaded object failed post-upload verification")
        return True

    def publish(self, item: PublishInput) -> PublishResult:
        archive_exists = self._immutable_exists(
            item.archive_key,
            expected_size=item.archive_size_bytes,
            expected_sha256=item.archive_sha256,
        )
        archive_uploaded = False
        if not archive_exists:
            self._multipart_upload(item)
            archive_uploaded = True

        manifest_uploaded = self._put_small(
            key=item.manifest_key,
            payload=item.manifest_bytes,
            content_type="application/json",
            immutable=True,
        )

        # This is intentionally the final provider mutation.  The object is
        # small and a single PUT is atomic from an object-store reader's view.
        channel_payload = build_channel_manifest(item)
        channel_digest = hashlib.sha256(channel_payload).hexdigest()
        channel_response = self._head(item.channel_key)
        channel_updated = False
        if channel_response is None or not self._head_matches(
            channel_response,
            expected_size=len(channel_payload),
            expected_sha256=channel_digest,
        ):
            self._put_small(
                key=item.channel_key,
                payload=channel_payload,
                content_type="application/json",
                immutable=False,
            )
            channel_updated = True

        return PublishResult(
            status="published",
            channel=item.channel,
            archive_key=item.archive_key,
            manifest_key=item.manifest_key,
            channel_key=item.channel_key,
            archive_size_bytes=item.archive_size_bytes,
            archive_sha256=item.archive_sha256,
            manifest_sha256=item.manifest_sha256,
            archive_uploaded=archive_uploaded,
            manifest_uploaded=manifest_uploaded,
            channel_updated=channel_updated,
        )


def _configuration_from_args(args: argparse.Namespace) -> StoreConfig | None:
    endpoint = args.endpoint or _env_value("OBJECT_STORE_ENDPOINT", "S3_ENDPOINT", "R2_ENDPOINT")
    bucket = args.bucket or _env_value("OBJECT_STORE_BUCKET", "S3_BUCKET", "R2_BUCKET")
    access_key_id = args.access_key_id or _env_value(
        "OBJECT_STORE_ACCESS_KEY_ID", "S3_ACCESS_KEY_ID", "R2_ACCESS_KEY_ID"
    )
    secret_access_key = args.secret_access_key or _env_value(
        "OBJECT_STORE_SECRET_ACCESS_KEY", "S3_SECRET_ACCESS_KEY", "R2_SECRET_ACCESS_KEY"
    )
    region = args.region or _env_value("OBJECT_STORE_REGION", "S3_REGION", "R2_REGION") or "auto"
    prefix = args.prefix if args.prefix is not None else (_env_value("OBJECT_STORE_PREFIX", "S3_PREFIX", "R2_PREFIX") or "")
    supplied = [endpoint, bucket, access_key_id, secret_access_key]
    present = sum(value is not None and value != "" for value in supplied)
    if present == 0:
        return None
    if present != len(supplied):
        raise RuntimePublisherError("object-store configuration is partial; refusing to upload")
    config = StoreConfig(
        endpoint=cast(str, endpoint),
        bucket=cast(str, bucket),
        access_key_id=cast(str, access_key_id),
        secret_access_key=cast(str, secret_access_key),
        region=region,
        prefix=prefix,
    )
    config.validate()
    return config


def _prefix_from_args(args: argparse.Namespace) -> str:
    if args.prefix is not None:
        return args.prefix
    return _env_value("OBJECT_STORE_PREFIX", "S3_PREFIX", "R2_PREFIX") or ""


def _build_client(config: StoreConfig) -> ObjectStoreClient:
    if boto3 is None or BotoConfig is None:
        raise RuntimePublisherError("boto3 is required for object-store publishing")
    try:
        client = boto3.client(
            "s3",
            endpoint_url=config.endpoint,
            region_name=config.region,
            aws_access_key_id=config.access_key_id,
            aws_secret_access_key=config.secret_access_key,
            config=BotoConfig(
                signature_version="s3v4",
                retries={"mode": "standard", "max_attempts": 3},
                connect_timeout=30,
                read_timeout=300,
                max_pool_connections=2,
            ),
        )
    except Exception as error:
        raise RuntimePublisherError("object-store client initialization failed") from error
    return cast(ObjectStoreClient, client)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True, help="verified sha256-<digest>.tar.zst archive")
    parser.add_argument("--manifest", type=Path, required=True, help="runtime manifest for the archive")
    parser.add_argument("--channel", default="staging", help="mutable channel pointer name (default: staging)")
    parser.add_argument("--endpoint", help="HTTPS S3-compatible endpoint (or OBJECT_STORE_ENDPOINT)")
    parser.add_argument("--bucket", help="object-store bucket (or OBJECT_STORE_BUCKET)")
    parser.add_argument("--access-key-id", help="access key (or OBJECT_STORE_ACCESS_KEY_ID)")
    parser.add_argument("--secret-access-key", help="secret key (or OBJECT_STORE_SECRET_ACCESS_KEY)")
    parser.add_argument("--region", help="signing region (default: auto)")
    parser.add_argument("--prefix", help="optional safe object-key prefix")
    parser.add_argument("--part-size-mib", type=int, choices=ALLOWED_PART_SIZE_MIB, default=128)
    parser.add_argument("--dry-run", action="store_true", help="validate and print the plan without network writes")
    parser.add_argument("--require-config", action="store_true", help="fail when object-store credentials are absent")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config = _configuration_from_args(args)
        # A normal public build must be able to include this helper without
        # paying the cost of hashing a multi-gigabyte archive when deployment
        # credentials are intentionally absent.  Explicit dry-runs still
        # validate all local inputs and show the derived plan.
        if config is None and args.require_config:
            raise RuntimePublisherError("object-store credentials are not configured")
        if config is None and not args.dry_run:
            print(json.dumps({"status": "skipped", "reason": "object-store credentials are not configured"}, separators=(",", ":")))
            return 0
        item = prepare_publish(args.archive, args.manifest, prefix=_prefix_from_args(args), channel=args.channel)
        if config is None:
            result = PublishResult(
                status="dry-run",
                channel=item.channel,
                archive_key=item.archive_key,
                manifest_key=item.manifest_key,
                channel_key=item.channel_key,
                archive_size_bytes=item.archive_size_bytes,
                archive_sha256=item.archive_sha256,
                manifest_sha256=item.manifest_sha256,
            )
        elif args.dry_run:
            result = PublishResult(
                status="dry-run",
                channel=item.channel,
                archive_key=item.archive_key,
                manifest_key=item.manifest_key,
                channel_key=item.channel_key,
                archive_size_bytes=item.archive_size_bytes,
                archive_sha256=item.archive_sha256,
                manifest_sha256=item.manifest_sha256,
            )
        else:
            result = RuntimePublisher(
                _build_client(config),
                config,
                part_size_bytes=args.part_size_mib * MIB,
            ).publish(item)
        print(json.dumps(result.as_json(), ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return 0
    except (RuntimePublisherError, OSError) as error:
        print(f"runtime publication failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
