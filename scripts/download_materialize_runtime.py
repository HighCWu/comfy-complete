#!/usr/bin/env python3
"""Download one immutable runtime bundle and publish it to a mounted volume.

This is the entrypoint for the small CPU runtime-materializer image.  The
caller supplies two short-lived HTTPS URLs and the expected byte counts and
SHA-256 digests through environment variables.  The downloader never sends
credentials or provider-specific headers.  It verifies the manifest before
downloading the archive, then delegates the final archive -> volume operation
to :mod:`materialize_runtime`, which owns the lock and atomic ``current``
publication contract.

The command deliberately emits only bounded JSON status records.  URLs,
filesystem paths, response bodies, and exception details are not written to
stdout or stderr.  A failed download or materialization exits with status 2;
an existing ``current`` generation is left to the provider-neutral
materializer and is never replaced by a partial download.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import ssl
import stat
import tempfile
from typing import Any, Mapping, Sequence
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

from materialize_runtime import RuntimeMaterializerError, materialize_runtime
from runtime_manifest import RuntimeManifest, RuntimeManifestError, validate_manifest


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DEFAULT_VOLUME_ROOT = "/runpod-volume"
DEFAULT_DOWNLOAD_TIMEOUT_SECONDS = 120.0
MAX_DOWNLOAD_TIMEOUT_SECONDS = 900.0
MAX_REDIRECTS = 4
MAX_URL_LENGTH = 4096
MAX_MANIFEST_BYTES = 128 * 1024 * 1024
# This bound is intentionally generous for the current ~16GB runtime while
# still rejecting an accidentally unbounded value before creating a file.
MAX_ARCHIVE_BYTES = 1024 * 1024 * 1024 * 1024
READ_CHUNK_BYTES = 8 * 1024 * 1024


class RuntimeDownloadError(RuntimeError):
    """A bounded, path-free error returned by the image entrypoint."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _error(code: str) -> RuntimeDownloadError:
    return RuntimeDownloadError(code)


def _validate_url(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_URL_LENGTH:
        raise _error("configuration_invalid")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise _error("configuration_invalid")
    try:
        parsed = urlparse.urlsplit(value)
        hostname = parsed.hostname
        # Accessing ``port`` catches malformed values such as ``:notaport``.
        port = parsed.port
    except ValueError as error:
        del error
        raise _error("configuration_invalid") from None
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or port is not None and not 1 <= port <= 65535
    ):
        raise _error("https_required")
    return value


def _parse_positive_integer(name: str, value: object, *, maximum: int) -> int:
    if not isinstance(value, str) or not value or not value.isascii() or not value.isdecimal():
        raise _error("configuration_invalid")
    try:
        parsed = int(value, 10)
    except ValueError as error:
        del error
        raise _error("configuration_invalid") from None
    if parsed <= 0 or parsed > maximum:
        raise _error("configuration_invalid")
    return parsed


def _parse_sha256(name: str, value: object) -> str:
    del name
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise _error("configuration_invalid")
    return value


def _parse_timeout(value: object) -> float:
    if value is None or value == "":
        return DEFAULT_DOWNLOAD_TIMEOUT_SECONDS
    if not isinstance(value, str):
        raise _error("configuration_invalid")
    try:
        timeout = float(value)
    except ValueError as error:
        del error
        raise _error("configuration_invalid") from None
    if timeout < 1.0 or timeout > MAX_DOWNLOAD_TIMEOUT_SECONDS:
        raise _error("configuration_invalid")
    return timeout


@dataclass(frozen=True)
class RuntimeDownloadConfig:
    archive_url: str
    manifest_url: str
    archive_sha256: str
    archive_size_bytes: int
    manifest_sha256: str
    manifest_size_bytes: int
    volume_root: Path
    timeout_seconds: float

    @classmethod
    def from_environment(cls, *, volume_root_override: str | None = None) -> "RuntimeDownloadConfig":
        archive_url = _validate_url(os.environ.get("RUNTIME_ARCHIVE_URL"))
        manifest_url = _validate_url(os.environ.get("RUNTIME_MANIFEST_URL"))
        archive_sha256 = _parse_sha256(
            "RUNTIME_ARCHIVE_SHA256", os.environ.get("RUNTIME_ARCHIVE_SHA256")
        )
        manifest_sha256 = _parse_sha256(
            "RUNTIME_MANIFEST_SHA256", os.environ.get("RUNTIME_MANIFEST_SHA256")
        )
        archive_size_bytes = _parse_positive_integer(
            "RUNTIME_ARCHIVE_SIZE_BYTES",
            os.environ.get("RUNTIME_ARCHIVE_SIZE_BYTES"),
            maximum=MAX_ARCHIVE_BYTES,
        )
        manifest_size_bytes = _parse_positive_integer(
            "RUNTIME_MANIFEST_SIZE_BYTES",
            os.environ.get("RUNTIME_MANIFEST_SIZE_BYTES"),
            maximum=MAX_MANIFEST_BYTES,
        )
        configured_root = volume_root_override or os.environ.get(
            "RUNTIME_VOLUME_ROOT", DEFAULT_VOLUME_ROOT
        )
        root_path = Path(configured_root)
        if (
            not configured_root
            or "\x00" in configured_root
            or not root_path.is_absolute()
            or ".." in root_path.parts
        ):
            raise _error("configuration_invalid")
        return cls(
            archive_url=archive_url,
            manifest_url=manifest_url,
            archive_sha256=archive_sha256,
            archive_size_bytes=archive_size_bytes,
            manifest_sha256=manifest_sha256,
            manifest_size_bytes=manifest_size_bytes,
            volume_root=root_path,
            timeout_seconds=_parse_timeout(os.environ.get("RUNTIME_DOWNLOAD_TIMEOUT_SECONDS")),
        )


class _HttpsRedirectHandler(urlrequest.HTTPRedirectHandler):
    """Reject a redirect before urllib constructs the next request."""

    max_redirections = MAX_REDIRECTS

    def redirect_request(
        self,
        req: urlrequest.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urlrequest.Request | None:
        del fp, msg, headers
        _validate_url(newurl)
        return super().redirect_request(req, None, code, "", {}, newurl)


def _opener() -> urlrequest.OpenerDirector:
    # Ignore ambient proxy variables.  The image has no secret-bearing proxy
    # configuration and should connect only to the caller-supplied HTTPS URL.
    context = ssl.create_default_context()
    return urlrequest.build_opener(
        urlrequest.ProxyHandler({}),
        urlrequest.HTTPSHandler(context=context),
        _HttpsRedirectHandler(),
    )


def _response_status(response: Any) -> int:
    status = getattr(response, "status", None)
    if status is None:
        status = getattr(response, "code", None)
    if not isinstance(status, int) or status < 200 or status >= 300:
        raise _error("download_http")
    return status


def _content_length(response: Any) -> int | None:
    raw = response.headers.get("Content-Length")
    if raw is None:
        return None
    if not raw.isascii() or not raw.isdecimal():
        raise _error("download_headers_invalid")
    try:
        value = int(raw, 10)
    except ValueError as error:
        del error
        raise _error("download_headers_invalid") from None
    if value < 0:
        raise _error("download_headers_invalid")
    return value


def _download_file(
    url: str,
    destination: Path,
    *,
    expected_sha256: str,
    expected_size_bytes: int,
    timeout_seconds: float,
) -> None:
    """Stream one exact HTTPS object into *destination* without URL output."""

    try:
        request = urlrequest.Request(
            _validate_url(url),
            headers={
                "Accept-Encoding": "identity",
                "User-Agent": "comfy-runtime-materializer/1",
            },
            method="GET",
        )
        with _opener().open(request, timeout=timeout_seconds) as response:
            _response_status(response)
            _validate_url(response.geturl())
            advertised_size = _content_length(response)
            if advertised_size is not None and advertised_size != expected_size_bytes:
                raise _error("download_size_mismatch")

            digest = hashlib.sha256()
            size = 0
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("wb") as handle:
                while True:
                    chunk = response.read(READ_CHUNK_BYTES)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > expected_size_bytes:
                        raise _error("download_size_mismatch")
                    digest.update(chunk)
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            if size != expected_size_bytes or digest.hexdigest() != expected_sha256:
                raise _error("download_digest_mismatch")
    except RuntimeDownloadError:
        raise
    except (OSError, TimeoutError, ValueError, urlerror.URLError, urlerror.HTTPError) as error:
        del error
        raise _error("download_failed") from None


def _read_manifest(
    path: Path,
    *,
    expected_sha256: str,
    expected_size_bytes: int,
    archive_sha256: str,
    archive_size_bytes: int,
) -> tuple[bytes, RuntimeManifest]:
    try:
        payload = path.read_bytes()
    except OSError as error:
        del error
        raise _error("manifest_unavailable") from None
    if len(payload) != expected_size_bytes or hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise _error("manifest_digest_mismatch")
    try:
        manifest = validate_manifest(json.loads(payload.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError, RuntimeManifestError) as error:
        del error
        raise _error("manifest_invalid") from None
    archive = manifest["archive"]
    if archive["sha256"] != archive_sha256 or archive["size_bytes"] != archive_size_bytes:
        raise _error("manifest_archive_mismatch")
    return payload, manifest


def _validate_volume_root(path: Path) -> None:
    # Do not create the mount root here.  A missing mount must fail closed
    # instead of materializing into the CPU container's own root filesystem.
    try:
        metadata = path.lstat()
    except OSError as error:
        del error
        raise _error("volume_unavailable") from None
    if not stat.S_ISDIR(metadata.st_mode):
        raise _error("volume_unavailable")


def run(config: RuntimeDownloadConfig) -> Mapping[str, object]:
    """Download and materialize one runtime, returning bounded metadata."""

    _validate_volume_root(config.volume_root)
    try:
        free_bytes = shutil.disk_usage("/tmp").free
    except OSError as error:
        del error
        raise _error("download_disk_space") from None
    # Leave a modest amount of headroom for Python and the filesystem journal;
    # the archive itself is never copied a second time by this entrypoint.
    if free_bytes < config.archive_size_bytes + 64 * 1024 * 1024:
        raise _error("download_disk_space")

    with tempfile.TemporaryDirectory(prefix="runtime-materializer-", dir="/tmp") as directory:
        temporary_root = Path(directory)
        manifest_path = temporary_root / "manifest.json"
        _download_file(
            config.manifest_url,
            manifest_path,
            expected_sha256=config.manifest_sha256,
            expected_size_bytes=config.manifest_size_bytes,
            timeout_seconds=config.timeout_seconds,
        )
        manifest_bytes, manifest = _read_manifest(
            manifest_path,
            expected_sha256=config.manifest_sha256,
            expected_size_bytes=config.manifest_size_bytes,
            archive_sha256=config.archive_sha256,
            archive_size_bytes=config.archive_size_bytes,
        )
        del manifest_bytes
        archive_name = manifest["archive"]["object_name"]
        archive_path = temporary_root / archive_name
        _download_file(
            config.archive_url,
            archive_path,
            expected_sha256=config.archive_sha256,
            expected_size_bytes=config.archive_size_bytes,
            timeout_seconds=config.timeout_seconds,
        )
        try:
            result = materialize_runtime(archive_path, manifest_path, config.volume_root)
        except RuntimeMaterializerError as error:
            raise _error(error.code) from None
        except (OSError, ValueError) as error:
            del error
            raise _error("materialization_failed") from None

    # Copy only scalar fields from the provider-neutral materializer.  In
    # particular, do not pass through paths, manifest source metadata, or URL
    # values supplied by the caller.
    return {
        "status": result["status"],
        "runtime_digest": result["runtime_digest"],
        "archive_sha256": config.archive_sha256,
        "archive_size_bytes": config.archive_size_bytes,
        "manifest_sha256": config.manifest_sha256,
        "manifest_size_bytes": config.manifest_size_bytes,
        "entry_count": result["entry_count"],
        "current_updated": result["current_updated"],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--volume-root",
        default=None,
        help="test-only override; production defaults to RUNTIME_VOLUME_ROOT or /runpod-volume",
    )
    return parser


def _json_result(value: Mapping[str, object]) -> str:
    return json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config = RuntimeDownloadConfig.from_environment(volume_root_override=args.volume_root)
        print(_json_result(run(config)), flush=True)
        return 0
    except RuntimeDownloadError as error:
        print(_json_result({"status": "error", "error": error.code}), flush=True)
        return 2
    except (OSError, ValueError):
        print(_json_result({"status": "error", "error": "materializer_failed"}), flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
