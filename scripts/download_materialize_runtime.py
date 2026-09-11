#!/usr/bin/env python3
"""Verify and publish one immutable runtime bundle to a mounted volume.

This is the entrypoint for the small CPU runtime-materializer image.  Product
hydration stages the archive and manifest on the mounted Network Volume before
starting this image.  In that mode the caller supplies deterministic absolute
paths under ``RUNTIME_VOLUME_ROOT`` plus the expected byte counts and SHA-256
digests through environment variables.  The entrypoint verifies the staged
manifest and path contract in place, while the provider-neutral materializer
performs the single authoritative archive verification; neither component
downloads, copies, or deletes the staged inputs.  A legacy HTTPS
downloader mode remains for old images and tests, but is not the product
hydration path.

After verification the entrypoint delegates the final archive -> volume
operation to :mod:`materialize_runtime`, which owns the lock and atomic
``current`` publication contract.

The command deliberately emits only bounded JSON status records.  URLs,
filesystem paths, response bodies, and exception details are not written to
stdout or stderr.  Phase/progress records go to stderr; the final result (or
bounded failure record) goes to stdout.  A failed verification or
materialization exits with status 2; an existing ``current`` generation is
left to the provider-neutral materializer and is never replaced by a partial
download.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import ssl
import stat
import sys
import tempfile
from typing import Any, Mapping, Sequence
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

from materialize_runtime import (
    RuntimeMaterializerError,
    materialize_runtime,
    probe_volume_mode_capability,
)
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
# Keep each request bounded while avoiding hundreds of requests for the current
# ~16 GB runtime archive.  The file is written sequentially, so this does not
# require a second archive-sized staging buffer.
ARCHIVE_RANGE_BYTES = 256 * 1024 * 1024
ARCHIVE_VERIFY_PROGRESS_BYTES = 256 * 1024 * 1024
MAX_RESULT_BYTES = 16 * 1024
CONTENT_RANGE_RE = re.compile(r"^bytes ([0-9]+)-([0-9]+)/([0-9]+)$")
TEMP_STORAGE_EXHAUSTION_ERRNOS = frozenset(
    value
    for value in (errno.ENOSPC, getattr(errno, "EDQUOT", None), getattr(errno, "EFBIG", None))
    if value is not None
)


class RuntimeDownloadError(RuntimeError):
    """A bounded, path-free error returned by the image entrypoint."""

    def __init__(self, code: str, *, diagnostics: Mapping[str, int] | None = None) -> None:
        self.code = code
        self.diagnostics = {
            key: value
            for key, value in (diagnostics or {}).items()
            if isinstance(key, str)
            and isinstance(value, int)
            and not isinstance(value, bool)
            and value >= 0
        }
        super().__init__(code)


def _error(
    code: str,
    *,
    diagnostics: Mapping[str, int] | None = None,
) -> RuntimeDownloadError:
    return RuntimeDownloadError(code, diagnostics=diagnostics)


def _is_storage_exhaustion(error: OSError) -> bool:
    return error.errno in TEMP_STORAGE_EXHAUSTION_ERRNOS


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


def _validate_staged_path(value: object, volume_root: Path) -> Path:
    """Validate one deterministic absolute path inside the mounted volume.

    The lexical checks happen while reading configuration so a malformed path
    cannot silently select legacy downloader mode.  The filesystem checks are
    repeated immediately before opening the file because a path component may
    have been replaced after configuration was parsed.
    """

    if not isinstance(value, str) or not value or "\x00" in value:
        raise _error("configuration_invalid")
    if not value.startswith(os.sep):
        raise _error("configuration_invalid")
    if "\\" in value:
        raise _error("configuration_invalid")
    # Path normalisation hides these components, so inspect the raw POSIX
    # spelling as well.  The image runs on Linux and the mount contract is
    # intentionally POSIX-only.
    if any(component in {".", ".."} for component in value.split(os.sep)):
        raise _error("configuration_invalid")
    path = Path(value)
    if not path.is_absolute() or str(path) != value or path == volume_root:
        raise _error("configuration_invalid")
    try:
        relative = path.relative_to(volume_root)
    except ValueError:
        raise _error("configuration_invalid") from None
    if not relative.parts:
        raise _error("configuration_invalid")
    return path


@dataclass(frozen=True)
class RuntimeDownloadConfig:
    archive_url: str | None
    manifest_url: str | None
    archive_sha256: str
    archive_size_bytes: int
    manifest_sha256: str
    manifest_size_bytes: int
    volume_root: Path
    timeout_seconds: float
    result_url: str | None = None
    archive_path: Path | None = None
    manifest_path: Path | None = None

    @property
    def is_pre_staged(self) -> bool:
        return self.archive_path is not None and self.manifest_path is not None

    @classmethod
    def from_environment(cls, *, volume_root_override: str | None = None) -> "RuntimeDownloadConfig":
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

        archive_path_value = os.environ.get("RUNTIME_ARCHIVE_PATH")
        manifest_path_value = os.environ.get("RUNTIME_MANIFEST_PATH")
        if (archive_path_value is None) != (manifest_path_value is None):
            # Do not fall back to a URL just because one half of a staged
            # pair is missing.  That would make a partially configured Pod
            # unexpectedly consume container-disk space and GPU time.
            raise _error("configuration_invalid")
        if archive_path_value is not None and manifest_path_value is not None:
            archive_path = _validate_staged_path(archive_path_value, root_path)
            manifest_path = _validate_staged_path(manifest_path_value, root_path)
            archive_url = None
            manifest_url = None
        else:
            archive_path = None
            manifest_path = None
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
        return cls(
            archive_url=archive_url,
            manifest_url=manifest_url,
            archive_sha256=archive_sha256,
            archive_size_bytes=archive_size_bytes,
            manifest_sha256=manifest_sha256,
            manifest_size_bytes=manifest_size_bytes,
            volume_root=root_path,
            timeout_seconds=_parse_timeout(os.environ.get("RUNTIME_DOWNLOAD_TIMEOUT_SECONDS")),
            result_url=(
                _validate_url(os.environ["RUNTIME_RESULT_URL"])
                if os.environ.get("RUNTIME_RESULT_URL")
                else None
            ),
            archive_path=archive_path,
            manifest_path=manifest_path,
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


def _emit_event(event: str, phase: str, **fields: object) -> None:
    """Emit one bounded, path-free progress record to stderr."""

    payload: dict[str, object] = {"event": event, "phase": phase}
    for key, value in fields.items():
        # Callers only pass fixed phase names and scalar counters.  Keep this
        # guard here so a future call site cannot accidentally log a URL, path,
        # exception, or arbitrary response body.
        if isinstance(value, bool):
            payload[key] = value
        elif isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            payload[key] = value
        elif isinstance(value, str) and len(value) <= 64 and "\n" not in value:
            payload[key] = value
    print(_json_result(payload), file=sys.stderr, flush=True)


def _validate_staged_file_path(path: Path, volume_root: Path) -> None:
    """Require *path* to be a regular, non-symlink file below *volume_root*."""

    try:
        relative = path.relative_to(volume_root)
    except ValueError:
        raise _error("staged_path_invalid") from None
    if not relative.parts:
        raise _error("staged_path_invalid")

    current = volume_root
    try:
        root_metadata = current.lstat()
        if not stat.S_ISDIR(root_metadata.st_mode) or stat.S_ISLNK(root_metadata.st_mode):
            raise _error("volume_unavailable")
        for component in relative.parts:
            current = current / component
            metadata = current.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise _error("staged_path_invalid")
            if current != path and not stat.S_ISDIR(metadata.st_mode):
                raise _error("staged_path_invalid")
        final_metadata = path.lstat()
        if not stat.S_ISREG(final_metadata.st_mode):
            raise _error("staged_path_invalid")
        resolved_root = volume_root.resolve(strict=True)
        resolved_path = path.resolve(strict=True)
        try:
            resolved_path.relative_to(resolved_root)
        except ValueError:
            raise _error("staged_path_invalid") from None
    except RuntimeDownloadError:
        raise
    except FileNotFoundError:
        raise _error("staged_input_unavailable") from None
    except OSError:
        raise _error("staged_path_invalid") from None


def _validate_staged_layout(config: RuntimeDownloadConfig) -> None:
    """Require the control-plane's content-addressed staging layout."""

    if config.archive_path is None or config.manifest_path is None:
        raise _error("configuration_invalid")
    expected_archive = (
        config.volume_root
        / ".runtime-incoming"
        / "archives"
        / f"sha256-{config.archive_sha256}.tar.zst"
    )
    expected_manifest = (
        config.volume_root
        / ".runtime-incoming"
        / "manifests"
        / f"sha256-{config.manifest_sha256}.json"
    )
    if config.archive_path != expected_archive or config.manifest_path != expected_manifest:
        raise _error("staged_path_invalid")


def _open_staged_file(path: Path, volume_root: Path) -> Any:
    """Open a pre-staged file after a no-symlink, in-volume validation."""

    _validate_staged_file_path(path, volume_root)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            os.close(descriptor)
            descriptor = -1
            raise _error("staged_path_invalid")
        return os.fdopen(descriptor, "rb")
    except RuntimeDownloadError:
        raise
    except FileNotFoundError:
        if descriptor >= 0:
            os.close(descriptor)
        raise _error("staged_input_unavailable") from None
    except OSError:
        if descriptor >= 0:
            os.close(descriptor)
        raise _error("staged_path_invalid") from None


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


def _content_range(response: Any) -> tuple[int, int, int]:
    raw = response.headers.get("Content-Range")
    if not isinstance(raw, str):
        raise _error("download_headers_invalid")
    match = CONTENT_RANGE_RE.fullmatch(raw)
    if match is None:
        raise _error("download_headers_invalid")
    try:
        start, end, total = (int(value, 10) for value in match.groups())
    except ValueError as error:
        del error
        raise _error("download_headers_invalid") from None
    if start < 0 or end < start or total <= 0 or end >= total:
        raise _error("download_headers_invalid")
    return start, end, total


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
    except OSError as error:
        if _is_storage_exhaustion(error):
            raise _error("archive_temporary_disk_exhausted") from error
        raise _error("download_failed") from None
    except (TimeoutError, ValueError, urlerror.URLError, urlerror.HTTPError) as error:
        del error
        raise _error("download_failed") from None


def _download_range_chunks(
    url: str,
    destination: Path,
    *,
    expected_sha256: str,
    expected_size_bytes: int,
    timeout_seconds: float,
) -> None:
    """Stream one exact object through contiguous, verified Range requests.

    A server that ignores Range and returns ``200`` is rejected.  Every
    accepted response must identify exactly the requested interval and total
    object size; the body is checked for both short and overlong responses
    before the next interval is requested.
    """

    if expected_size_bytes <= 0:
        raise _error("configuration_invalid")
    validated_url = _validate_url(url)
    opener = _opener()
    digest = hashlib.sha256()
    downloaded = 0
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("wb") as handle:
            while downloaded < expected_size_bytes:
                start = downloaded
                end = min(start + ARCHIVE_RANGE_BYTES - 1, expected_size_bytes - 1)
                chunk_size = end - start + 1
                request = urlrequest.Request(
                    validated_url,
                    headers={
                        "Accept-Encoding": "identity",
                        "Range": f"bytes={start}-{end}",
                        "User-Agent": "comfy-runtime-materializer/1",
                    },
                    method="GET",
                )
                with opener.open(request, timeout=timeout_seconds) as response:
                    status = _response_status(response)
                    if status != 206:
                        raise _error("download_range_required")
                    _validate_url(response.geturl())
                    response_start, response_end, response_total = _content_range(response)
                    if (
                        response_start != start
                        or response_end != end
                        or response_total != expected_size_bytes
                    ):
                        raise _error("download_range_mismatch")
                    advertised_size = _content_length(response)
                    if advertised_size != chunk_size:
                        raise _error("download_size_mismatch")

                    remaining = chunk_size
                    while remaining > 0:
                        chunk = response.read(min(READ_CHUNK_BYTES, remaining))
                        if not chunk:
                            raise _error("download_size_mismatch")
                        if len(chunk) > remaining:
                            raise _error("download_size_mismatch")
                        handle.write(chunk)
                        digest.update(chunk)
                        remaining -= len(chunk)
                    # A compliant response must not contain bytes beyond the
                    # advertised interval.  Reading one extra byte catches a
                    # body whose Content-Length was understated.
                    if response.read(1):
                        raise _error("download_size_mismatch")
                downloaded = end + 1
                if (
                    downloaded == expected_size_bytes
                    or downloaded == chunk_size
                    or downloaded % ARCHIVE_VERIFY_PROGRESS_BYTES < chunk_size
                ):
                    _emit_event(
                        "progress",
                        "archive_download",
                        downloaded_bytes=downloaded,
                        total_bytes=expected_size_bytes,
                    )
            handle.flush()
            os.fsync(handle.fileno())
    except RuntimeDownloadError:
        raise
    except OSError as error:
        if _is_storage_exhaustion(error):
            raise _error("archive_temporary_disk_exhausted") from error
        raise _error("download_failed") from None
    except (TimeoutError, ValueError, urlerror.URLError, urlerror.HTTPError) as error:
        del error
        raise _error("download_failed") from None
    if downloaded != expected_size_bytes or digest.hexdigest() != expected_sha256:
        raise _error("download_digest_mismatch")


def _post_result(url: str, payload: Mapping[str, object], *, timeout_seconds: float) -> None:
    """POST one bounded result to an optional HTTPS capability URL."""

    body = json.dumps(
        dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    if len(body) > MAX_RESULT_BYTES:
        raise _error("result_too_large")
    try:
        request = urlrequest.Request(
            _validate_url(url),
            data=body,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "comfy-runtime-materializer/1",
            },
            method="POST",
        )
        with _opener().open(request, timeout=timeout_seconds) as response:
            _response_status(response)
            # Read at most a bounded response body to release the connection;
            # the response is never parsed or copied into the result record.
            response.read(MAX_RESULT_BYTES)
    except RuntimeDownloadError:
        raise
    except (OSError, TimeoutError, ValueError, urlerror.URLError, urlerror.HTTPError) as error:
        del error
        raise _error("result_report_failed") from None


def _read_verified_file_bytes(
    path: Path,
    *,
    expected_sha256: str,
    expected_size_bytes: int,
    volume_root: Path | None = None,
) -> bytes:
    """Read one bounded file and verify its exact size and digest."""

    try:
        if volume_root is None:
            handle = path.open("rb")
        else:
            handle = _open_staged_file(path, volume_root)
        with handle:
            digest = hashlib.sha256()
            payload = bytearray()
            while True:
                chunk = handle.read(READ_CHUNK_BYTES)
                if not chunk:
                    break
                payload.extend(chunk)
                if len(payload) > expected_size_bytes:
                    raise _error("manifest_size_mismatch")
                digest.update(chunk)
    except RuntimeDownloadError:
        raise
    except OSError as error:
        del error
        raise _error("manifest_unavailable") from None
    if len(payload) != expected_size_bytes or digest.hexdigest() != expected_sha256:
        raise _error("manifest_digest_mismatch")
    return bytes(payload)


def _read_manifest(
    path: Path,
    *,
    expected_sha256: str,
    expected_size_bytes: int,
    archive_sha256: str,
    archive_size_bytes: int,
    volume_root: Path | None = None,
) -> tuple[bytes, RuntimeManifest]:
    payload = _read_verified_file_bytes(
        path,
        expected_sha256=expected_sha256,
        expected_size_bytes=expected_size_bytes,
        volume_root=volume_root,
    )
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


def _materializer_progress(
    phase: str,
    state: str,
    current: int | None,
    total: int | None,
) -> None:
    """Translate materializer progress to bounded, path-free JSON logs."""

    if state == "start":
        _emit_event("phase", f"{phase}_start")
    elif state == "end":
        fields: dict[str, object] = {}
        if current is not None:
            if phase == "archive_verify":
                fields["verified_bytes"] = current
            else:
                fields["completed"] = current
        if total is not None:
            if phase == "archive_verify":
                fields["total_bytes"] = total
            else:
                fields["total"] = total
        _emit_event("phase", f"{phase}_end", **fields)
    elif state == "progress" and phase == "archive_verify" and current is not None:
        _emit_event(
            "progress",
            phase,
            verified_bytes=current,
            **({"total_bytes": total} if total is not None else {}),
        )


def _materialize_verified(
    archive_path: Path,
    manifest_path: Path,
    volume_root: Path,
    mode_policy: object,
    verified_manifest_bytes: bytes,
) -> Mapping[str, object]:
    """Run the provider-neutral materializer with bounded phase records."""

    _emit_event("phase", "materialization_start")
    completed = False
    try:
        result = materialize_runtime(
            archive_path,
            manifest_path,
            volume_root,
            mode_policy=mode_policy,
            verified_manifest_bytes=verified_manifest_bytes,
            progress_callback=_materializer_progress,
        )
        completed = True
        return result
    except RuntimeMaterializerError as error:
        raise _error(error.code, diagnostics=error.diagnostics) from None
    except (OSError, ValueError) as error:
        del error
        raise _error("materialization_failed") from None
    finally:
        _emit_event("phase", "materialization_end", outcome="success" if completed else "error")


def run(config: RuntimeDownloadConfig) -> Mapping[str, object]:
    """Verify, materialize, and return bounded metadata.

    The pre-staged branch never uses container ``/tmp`` for runtime bytes and
    never deletes the two caller-owned input files.  The URL branch below is
    retained only for compatibility with older images and public downloader
    tests.
    """

    _validate_volume_root(config.volume_root)
    _emit_event("phase", "volume_validated")

    if config.is_pre_staged:
        # ``is_pre_staged`` is true only when both paths are present.  Keep the
        # explicit checks so a directly-constructed config cannot accidentally
        # select a partial or URL-backed branch.
        if config.archive_path is None or config.manifest_path is None:
            raise _error("configuration_invalid")
        _validate_staged_layout(config)
        archive_path = config.archive_path
        manifest_path = config.manifest_path
        _emit_event("phase", "manifest_verify_start")
        manifest_bytes, manifest = _read_manifest(
            manifest_path,
            expected_sha256=config.manifest_sha256,
            expected_size_bytes=config.manifest_size_bytes,
            archive_sha256=config.archive_sha256,
            archive_size_bytes=config.archive_size_bytes,
            volume_root=config.volume_root,
        )
        if manifest["archive"]["object_name"] != archive_path.name:
            raise _error("staged_archive_name_mismatch")
        # Validate both caller-owned inputs before touching materializer-owned
        # volume state in the mode capability probe.
        _validate_staged_file_path(archive_path, config.volume_root)
        _emit_event("phase", "manifest_verify_end")
        try:
            # Fail before extraction if the mounted Network Volume silently
            # normalizes POSIX mode bits.  The probe is private and
            # independent from any existing runtime/current generation; it is
            # removed by the helper on every outcome.
            mode_policy = probe_volume_mode_capability(config.volume_root, manifest)
        except RuntimeMaterializerError as error:
            raise _error(error.code, diagnostics=error.diagnostics) from None
        result = _materialize_verified(
            archive_path,
            manifest_path,
            config.volume_root,
            mode_policy,
            manifest_bytes,
        )
    else:
        if config.archive_url is None or config.manifest_url is None:
            raise _error("configuration_invalid")
        try:
            free_bytes = shutil.disk_usage("/tmp").free
        except OSError as error:
            del error
            raise _error("download_disk_space") from None
        # Leave a modest amount of headroom for Python and the filesystem
        # journal; the archive itself is never copied a second time by this
        # entrypoint.
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
            try:
                # Fail before the expensive archive transfer if the mounted
                # Network Volume silently normalizes POSIX mode bits.  The
                # probe is private and independent from any existing
                # runtime/current generation; it is removed by the helper on
                # every outcome.
                mode_policy = probe_volume_mode_capability(config.volume_root, manifest)
            except RuntimeMaterializerError as error:
                raise _error(error.code, diagnostics=error.diagnostics) from None
            archive_name = manifest["archive"]["object_name"]
            archive_path = temporary_root / archive_name
            _download_range_chunks(
                config.archive_url,
                archive_path,
                expected_sha256=config.archive_sha256,
                expected_size_bytes=config.archive_size_bytes,
                timeout_seconds=config.timeout_seconds,
            )
            result = _materialize_verified(
                archive_path,
                manifest_path,
                config.volume_root,
                mode_policy,
                manifest_bytes,
            )

    consumed_archive_sha256 = result.get("archive_sha256")
    consumed_archive_size = result.get("archive_size_bytes")
    if (
        not isinstance(consumed_archive_sha256, str)
        or SHA256_RE.fullmatch(consumed_archive_sha256) is None
        or not isinstance(consumed_archive_size, int)
        or isinstance(consumed_archive_size, bool)
        or consumed_archive_size < 0
        or consumed_archive_sha256 != config.archive_sha256
        or consumed_archive_size != config.archive_size_bytes
    ):
        raise _error("materialization_result_invalid")
    consumed_manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    # Copy only scalar fields from the provider-neutral materializer.  In
    # particular, do not pass through paths, manifest source metadata, or URL
    # values supplied by the caller.  Archive identity comes from the
    # materializer result, i.e. from the bytes it actually consumed.
    output: dict[str, object] = {
        "status": result["status"],
        "runtime_digest": result["runtime_digest"],
        "archive_sha256": consumed_archive_sha256,
        "archive_size_bytes": consumed_archive_size,
        "verified_archive_bytes": consumed_archive_size,
        "manifest_sha256": consumed_manifest_sha256,
        "manifest_size_bytes": len(manifest_bytes),
        "entry_count": result["entry_count"],
        "current_updated": result["current_updated"],
    }
    # Keep the old field only for the legacy URL branch.  The product's
    # pre-staged branch must not claim that a GPU container downloaded bytes.
    if not config.is_pre_staged:
        output["downloaded_bytes"] = config.archive_size_bytes
    materialized_bytes = result.get("materialized_bytes")
    if (
        not isinstance(materialized_bytes, int)
        or isinstance(materialized_bytes, bool)
        or materialized_bytes < 0
    ):
        raise _error("materialization_result_invalid")
    output["materialized_bytes"] = materialized_bytes
    # Keep the result contract scalar and bounded while forwarding optional
    # filesystem snapshots added by the provider-neutral materializer.  Older
    # materializer images simply omit these fields.
    for key in (
        "volume_total_bytes",
        "volume_free_bytes_before",
        "volume_free_bytes_after",
        "volume_total_inodes",
        "volume_free_inodes_before",
        "volume_free_inodes_after",
        "hardlink_count",
        "hardlink_saved_bytes",
    ):
        value = result.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            output[key] = value
    return output


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
    config: RuntimeDownloadConfig | None = None
    try:
        config = RuntimeDownloadConfig.from_environment(volume_root_override=args.volume_root)
        result = run(config)
    except RuntimeDownloadError as error:
        error_payload: dict[str, object] = {"ok": False, "error_code": error.code}
        if error.diagnostics:
            error_payload["diagnostics"] = error.diagnostics
        if config is not None and config.result_url is not None:
            try:
                _post_result(
                    config.result_url,
                    error_payload,
                    timeout_seconds=config.timeout_seconds,
                )
            except RuntimeDownloadError:
                # The original materialization error remains authoritative.
                # In particular, a failed result report can never turn a
                # failed materialization into a success.
                pass
        output_payload: dict[str, object] = {"status": "error", "error": error.code}
        if error.diagnostics:
            output_payload["diagnostics"] = error.diagnostics
        print(_json_result(output_payload), flush=True)
        return 2
    except (OSError, ValueError):
        if config is not None and config.result_url is not None:
            try:
                _post_result(
                    config.result_url,
                    {"ok": False, "error_code": "materializer_failed"},
                    timeout_seconds=config.timeout_seconds,
                )
            except RuntimeDownloadError:
                pass
        print(_json_result({"status": "error", "error": "materializer_failed"}), flush=True)
        return 2

    success_payload = {"ok": True, **result}
    if config.result_url is not None:
        try:
            _post_result(
                config.result_url,
                success_payload,
                timeout_seconds=config.timeout_seconds,
            )
        except RuntimeDownloadError:
            # The volume may already contain the newly published generation,
            # but the caller must not observe a successful run without the
            # capability callback.  Exit fail-closed and do not print the
            # success payload.
            print(_json_result({"status": "error", "error": "result_report_failed"}), flush=True)
            return 2
    print(_json_result(success_payload), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
