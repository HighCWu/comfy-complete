"""Hydrate one immutable public model into a shared RunPod Network Volume.

The control plane owns source selection and the D1 writer lease. This process
receives only a short-lived lease token, publishes one content-addressed
artifact, reports bounded transfer/capacity metrics, and exits. The surrounding
RunPod CPU Pod must always be terminated by the control plane afterwards.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


TOKEN_HEADER = "X-Laimon-Hydration-Token"
SHA256_HEX = re.compile(r"^[a-f0-9]{64}$")
SAFE_ARTIFACT_PATH = re.compile(
    r"^shared/models/objects/[a-f0-9]{2}/[a-f0-9]{64}/artifact$"
)
CHUNK_BYTES = 8 * 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024
MAX_ERROR_BYTES = 2 * 1024


def trusted_huggingface_host(hostname: str) -> bool:
    return (
        hostname == "huggingface.co"
        or hostname.endswith(".hf.co")
        or hostname.endswith(".huggingface.co")
    )


class TrustedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Allow source redirects only to HTTPS Hugging Face infrastructure."""

    def redirect_request(
        self,
        request: urllib.request.Request,
        file_pointer: Any,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> urllib.request.Request | None:
        parsed = urllib.parse.urlsplit(new_url)
        if parsed.scheme != "https" or not parsed.hostname or not trusted_huggingface_host(
            parsed.hostname
        ):
            raise HydrationError(
                "source_redirect_untrusted",
                "Hugging Face redirected to an untrusted download host",
            )
        return super().redirect_request(
            request, file_pointer, code, message, headers, new_url
        )


class HydrationError(RuntimeError):
    """Terminal, low-cardinality hydration failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise HydrationError("configuration_missing", f"{name} is required")
    return value


def bounded_json(response: Any, maximum_bytes: int) -> dict[str, Any]:
    payload = response.read(maximum_bytes + 1)
    if len(payload) > maximum_bytes:
        raise HydrationError("control_response_too_large", "control response is too large")
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HydrationError("control_response_invalid", "control response is not JSON") from error
    if not isinstance(value, dict):
        raise HydrationError("control_response_invalid", "control response is not an object")
    return value


def control_request(
    url: str,
    token: str,
    *,
    method: str = "GET",
    body: dict[str, object] | None = None,
) -> Any:
    encoded = None if body is None else json.dumps(body, separators=(",", ":")).encode()
    headers = {
        TOKEN_HEADER: token,
        "Accept": "application/json",
        "User-Agent": "laimon-model-hydrator/1",
    }
    if encoded is not None:
        headers["Content-Type"] = "application/json"
    return urllib.request.urlopen(
        urllib.request.Request(url, data=encoded, headers=headers, method=method),
        timeout=60,
    )


def load_manifest(control_plane: str, hydration_id: str, token: str) -> dict[str, Any]:
    url = urllib.parse.urljoin(
        control_plane.rstrip("/") + "/",
        "/api/internal/model-hydrations/"
        + urllib.parse.quote(hydration_id, safe="")
        + "/manifest",
    )
    try:
        with control_request(url, token) as response:
            manifest = bounded_json(response, MAX_MANIFEST_BYTES)
    except urllib.error.HTTPError as error:
        detail = error.read(MAX_ERROR_BYTES).decode("utf-8", "replace")
        raise HydrationError(
            "manifest_rejected",
            f"control plane rejected hydration manifest ({error.code}): {detail}",
        ) from error
    if manifest.get("version") != 1 or manifest.get("hydration_id") != hydration_id:
        raise HydrationError("manifest_invalid", "hydration manifest binding is invalid")
    return manifest


def validate_manifest(value: dict[str, Any]) -> dict[str, object]:
    sha256 = value.get("sha256")
    size_bytes = value.get("size_bytes")
    artifact_path = value.get("artifact_path")
    marker_path = value.get("marker_path")
    reserve_bytes = value.get("reserve_bytes")
    overhead_bps = value.get("overhead_bps")
    source = value.get("source")
    if (
        not isinstance(sha256, str)
        or not SHA256_HEX.fullmatch(sha256)
        or not isinstance(size_bytes, int)
        or isinstance(size_bytes, bool)
        or size_bytes <= 0
        or not isinstance(artifact_path, str)
        or not SAFE_ARTIFACT_PATH.fullmatch(artifact_path)
        or artifact_path.split("/")[3] != sha256[:2]
        or artifact_path.split("/")[4] != sha256
        or marker_path != artifact_path + ".laimon.json"
        or not isinstance(reserve_bytes, int)
        or isinstance(reserve_bytes, bool)
        or reserve_bytes < 0
        or not isinstance(overhead_bps, int)
        or isinstance(overhead_bps, bool)
        or not 0 <= overhead_bps <= 10_000
        or not isinstance(source, dict)
    ):
        raise HydrationError("manifest_invalid", "hydration manifest metadata is invalid")
    kind = source.get("kind")
    download_url = source.get("download_url")
    if kind not in {"huggingface", "r2"} or not isinstance(download_url, str):
        raise HydrationError("manifest_invalid", "hydration source is invalid")
    try:
        parsed_url = urllib.parse.urlsplit(download_url)
    except ValueError as error:
        raise HydrationError("manifest_invalid", "hydration source URL is invalid") from error
    if parsed_url.scheme != "https" or not parsed_url.hostname:
        raise HydrationError("manifest_invalid", "hydration source must use HTTPS")
    if kind == "huggingface" and not trusted_huggingface_host(parsed_url.hostname):
        raise HydrationError("manifest_invalid", "Hugging Face source host is not trusted")
    return {
        "sha256": sha256,
        "size_bytes": size_bytes,
        "artifact_path": artifact_path,
        "marker_path": marker_path,
        "reserve_bytes": reserve_bytes,
        "overhead_bps": overhead_bps,
        "source_kind": kind,
        "download_url": download_url,
    }


def capacity(path: Path) -> dict[str, int]:
    stats = os.statvfs(path)
    return {
        "total_bytes": stats.f_frsize * stats.f_blocks,
        "free_bytes": stats.f_frsize * stats.f_bavail,
    }


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def existing_artifact_ready(
    artifact: Path,
    marker: Path,
    relative_artifact: str,
    expected_sha256: str,
    expected_size: int,
) -> bool:
    if not artifact.is_file() or not marker.is_file() or artifact.stat().st_size != expected_size:
        return False
    try:
        published = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return published == {
        "version": 1,
        "sha256": expected_sha256,
        "size_bytes": expected_size,
        "artifact_path": relative_artifact,
    }


def download(
    url: str,
    partial: Path,
    expected_size: int,
    source_kind: str,
    token: str,
) -> int:
    if partial.exists() and partial.stat().st_size > expected_size:
        partial.unlink()
    offset = partial.stat().st_size if partial.exists() else 0
    headers = {
        "Accept": "application/octet-stream",
        "User-Agent": "laimon-model-hydrator/1",
    }
    if source_kind == "r2":
        headers[TOKEN_HEADER] = token
    if offset:
        headers["Range"] = f"bytes={offset}-"
    request = urllib.request.Request(url, headers=headers)
    opener = (
        urllib.request.build_opener(TrustedRedirectHandler())
        if source_kind == "huggingface"
        else urllib.request.build_opener()
    )
    try:
        response = opener.open(request, timeout=120)
    except urllib.error.HTTPError as error:
        raise HydrationError("source_rejected", f"model source returned HTTP {error.code}") from error
    with response:
        status = getattr(response, "status", response.getcode())
        if offset and status != 206:
            offset = 0
            partial.unlink(missing_ok=True)
        mode = "ab" if offset and status == 206 else "wb"
        with partial.open(mode) as handle:
            while chunk := response.read(CHUNK_BYTES):
                handle.write(chunk)
    actual_size = partial.stat().st_size
    if actual_size != expected_size:
        raise HydrationError(
            "source_size_mismatch",
            f"expected {expected_size} bytes, received {actual_size}",
        )
    return actual_size


def hydrate(
    volume_root: Path,
    manifest: dict[str, object],
    token: str = "",
) -> dict[str, object]:
    volume_root = volume_root.resolve(strict=True)
    relative_artifact = str(manifest["artifact_path"])
    relative_marker = str(manifest["marker_path"])
    artifact = volume_root / relative_artifact
    marker = volume_root / relative_marker
    if not artifact.resolve().is_relative_to(volume_root) or not marker.resolve().is_relative_to(volume_root):
        raise HydrationError("path_escape", "hydration path escaped the mounted volume")
    artifact.parent.mkdir(parents=True, exist_ok=True)
    expected_sha256 = str(manifest["sha256"])
    expected_size = int(manifest["size_bytes"])
    before = capacity(volume_root)
    if existing_artifact_ready(
        artifact,
        marker,
        relative_artifact,
        expected_sha256,
        expected_size,
    ):
        return {
            "status": "already_ready",
            "source_kind": str(manifest["source_kind"]),
            "downloaded_bytes": 0,
            "download_ms": 0,
            "verify_ms": 0,
            "capacity_before": before,
            "capacity_after": before,
        }

    overhead = (expected_size * int(manifest["overhead_bps"]) + 9_999) // 10_000
    required_free = expected_size + overhead + int(manifest["reserve_bytes"])
    if before["free_bytes"] < required_free:
        raise HydrationError(
            "insufficient_volume_capacity",
            f"volume has {before['free_bytes']} free bytes; {required_free} required",
        )

    partial = artifact.with_name("artifact.partial")
    marker_temporary = marker.with_name("artifact.laimon.json.tmp")
    marker.unlink(missing_ok=True)
    marker_temporary.unlink(missing_ok=True)
    started = time.monotonic()
    downloaded_bytes = download(
        str(manifest["download_url"]),
        partial,
        expected_size,
        str(manifest["source_kind"]),
        token,
    )
    download_ms = round((time.monotonic() - started) * 1000)
    verify_started = time.monotonic()
    actual_sha256 = file_sha256(partial)
    verify_ms = round((time.monotonic() - verify_started) * 1000)
    if actual_sha256 != expected_sha256:
        partial.unlink(missing_ok=True)
        raise HydrationError("source_sha256_mismatch", "downloaded model SHA-256 is invalid")

    os.replace(partial, artifact)
    marker_payload = {
        "version": 1,
        "sha256": expected_sha256,
        "size_bytes": expected_size,
        "artifact_path": relative_artifact,
    }
    marker_temporary.write_text(
        json.dumps(marker_payload, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    os.replace(marker_temporary, marker)
    return {
        "status": "ready",
        "source_kind": str(manifest["source_kind"]),
        "downloaded_bytes": downloaded_bytes,
        "download_ms": download_ms,
        "verify_ms": verify_ms,
        "capacity_before": before,
        "capacity_after": capacity(volume_root),
    }


def report(
    control_plane: str,
    hydration_id: str,
    token: str,
    result: dict[str, object],
) -> None:
    url = urllib.parse.urljoin(
        control_plane.rstrip("/") + "/",
        "/api/internal/model-hydrations/"
        + urllib.parse.quote(hydration_id, safe="")
        + "/result",
    )
    with control_request(url, token, method="POST", body=result) as response:
        bounded_json(response, MAX_MANIFEST_BYTES)


def main() -> None:
    control_plane = required_env("LAIMON_CONTROL_PLANE_URL")
    hydration_id = required_env("LAIMON_HYDRATION_ID")
    token = required_env("LAIMON_HYDRATION_TOKEN")
    volume_root = Path(os.environ.get("LAIMON_VOLUME_ROOT", "/runpod-volume"))
    try:
        manifest = validate_manifest(load_manifest(control_plane, hydration_id, token))
        result = {"ok": True, **hydrate(volume_root, manifest, token)}
    except HydrationError as error:
        result = {"ok": False, "error_code": error.code, "message": str(error)[:500]}
    try:
        report(control_plane, hydration_id, token, result)
    except Exception as error:
        raise SystemExit(f"laimon-hydrator: failed to report terminal result: {error}") from error
    if not result["ok"]:
        raise SystemExit(f"laimon-hydrator: {result['error_code']}: {result['message']}")
    print(json.dumps(result, separators=(",", ":")), flush=True)


if __name__ == "__main__":
    main()
