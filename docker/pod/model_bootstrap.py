"""Prepare the exact model set bound to the active Compute Pod start quote.

The control plane authenticates this container with the lease-derived Pod
token and streams private R2 objects without exposing R2 credentials or keys.
Downloads are resumable, content-verified, and atomically published into a
fixed per-Pod object store before ComfyUI starts. Instance model names are
aliases to those content-addressed objects.
"""

from __future__ import annotations

import argparse
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


TOKEN_HEADER = "X-Comfy-Pod-Token"
SHA256_HEX = re.compile(r"^[a-f0-9]{64}$")
MODEL_OBJECT_ROOT = Path("/tmp/comfy-model-objects")
CHUNK_BYTES = 4 * 1024 * 1024
MANIFEST_WAIT_SECONDS = 300


class BootstrapError(RuntimeError):
    """Terminal model-bootstrap failure."""


def integration_configured() -> bool:
    """Return whether the optional control-plane integration is configured."""
    names = ("COMFY_CONTROL_PLANE_URL", "COMFY_INSTANCE_ID", "COMFY_POD_TOKEN")
    configured = [bool(os.environ.get(name, "").strip()) for name in names]
    if any(configured) and not all(configured):
        raise BootstrapError("control-plane variables must be configured together")
    return all(configured)


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise BootstrapError(f"{name} is required")
    return value


def safe_filename(value: str) -> bool:
    return safe_relative_path(value)


def safe_relative_path(value: str) -> bool:
    """Validate a POSIX relative path without resolving or cleaning it."""
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 2048
        or value.startswith("/")
        or value.endswith("/")
        or "//" in value
        or "\\" in value
        or "\x00" in value
    ):
        return False
    parts = value.split("/")
    return all(
        part and len(part) <= 255 and part not in {".", ".."}
        for part in parts
    )


def canonical_model_relative_path(folder: str, filename: str) -> str:
    if not safe_relative_path(folder) or not safe_relative_path(filename):
        raise BootstrapError("model path is unsafe")
    relative = f"{folder}/{filename}"
    if len(relative) > 2048:
        raise BootstrapError("model path is too long")
    return relative


def request(
    url: str,
    token: str,
    range_start: int | None = None,
) -> urllib.response.addinfourl:
    headers = {
        TOKEN_HEADER: token,
        "Accept": "application/json" if range_start is None else "application/octet-stream",
        "User-Agent": "comfy-pod-model-bootstrap/1",
    }
    if range_start is not None and range_start > 0:
        headers["Range"] = f"bytes={range_start}-"
    return urllib.request.urlopen(
        urllib.request.Request(url, headers=headers),
        timeout=60,
    )


def load_manifest(control_plane: str, instance_id: str, token: str) -> dict[str, Any]:
    manifest_url = urllib.parse.urljoin(
        control_plane.rstrip("/") + "/",
        "/api/internal/pod-models/manifest?instance_id="
        + urllib.parse.quote(instance_id, safe=""),
    )
    deadline = time.monotonic() + MANIFEST_WAIT_SECONDS
    last_error = "control plane not ready"
    while time.monotonic() < deadline:
        try:
            with request(manifest_url, token) as response:
                payload = json.load(response)
            if not isinstance(payload, dict) or payload.get("version") != 1:
                raise BootstrapError("model manifest has an unsupported shape")
            if payload.get("instance_id") != instance_id:
                raise BootstrapError("model manifest instance binding changed")
            return payload
        except urllib.error.HTTPError as error:
            detail = error.read(500).decode("utf-8", "replace")
            if error.code == 401:
                raise BootstrapError("control plane rejected the Pod token") from error
            if error.code not in {404, 409, 425, 503}:
                raise BootstrapError(
                    f"model manifest failed ({error.code}): {detail}"
                ) from error
            last_error = f"HTTP {error.code}: {detail}"
        except urllib.error.URLError as error:
            last_error = str(error.reason)
        print(f"comfy-pod: waiting for model manifest — {last_error}", flush=True)
        time.sleep(2)
    raise BootstrapError(f"model manifest did not become ready: {last_error}")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def model_object_path(
    item: dict[str, object],
    object_root: Path = MODEL_OBJECT_ROOT,
) -> Path:
    """Return the stable per-Pod path for a verified R2 object."""
    sha256 = str(item["sha256"])
    return object_root / sha256[:2] / sha256 / "artifact"


def link_model_alias(
    model_root: Path,
    item: dict[str, object],
    source: Path,
) -> None:
    """Expose one verified object under the instance's ComfyUI model name."""
    folder = str(item["folder"])
    filename = str(item["filename"])
    target_dir = model_root / folder
    target = target_dir / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    expected = source.absolute()
    if target.is_symlink():
        try:
            if target.resolve(strict=False) == expected.resolve(strict=False):
                return
        except OSError:
            pass
        raise BootstrapError(f"model alias collision for {folder}/{filename}")
    if target.exists():
        raise BootstrapError(f"model alias collision for {folder}/{filename}")
    temporary = target.with_name(f".{target.name}.model-alias")
    temporary.unlink(missing_ok=True)
    try:
        temporary.symlink_to(expected)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def validate_item(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise BootstrapError("model manifest item is not an object")
    folder = value.get("folder")
    filename = value.get("filename")
    size_bytes = value.get("size_bytes")
    sha256 = value.get("sha256")
    source = value.get("source")
    download_path = value.get("download_path")
    shared_volume_path = value.get("shared_volume_path")
    shared_marker_path = value.get("shared_marker_path")
    if (
        not isinstance(folder, str)
        or not safe_relative_path(folder)
        or not isinstance(filename, str)
        or not safe_filename(filename)
        or not isinstance(size_bytes, int)
        or isinstance(size_bytes, bool)
        or size_bytes <= 0
        or not isinstance(sha256, str)
        or not SHA256_HEX.fullmatch(sha256)
        or source not in {"r2", "shared_volume"}
    ):
        raise BootstrapError("model manifest item contains unsafe metadata")
    expected_shared_path = "models/" + canonical_model_relative_path(folder, filename)
    if source == "r2" and (
        not isinstance(download_path, str)
        or not download_path.startswith("/api/internal/pod-models/artifacts/")
        or shared_volume_path is not None
        or shared_marker_path is not None
    ):
        raise BootstrapError("R2 model manifest item contains unsafe metadata")
    if source == "shared_volume" and (
        download_path is not None
        or not isinstance(shared_volume_path, str)
        or shared_volume_path != expected_shared_path
        or not isinstance(shared_marker_path, str)
        or shared_marker_path != shared_volume_path + ".manifest.json"
    ):
        raise BootstrapError("shared model manifest item contains unsafe metadata")
    return {
        "folder": folder,
        "filename": filename,
        "size_bytes": size_bytes,
        "sha256": sha256,
        "source": source,
        "download_path": download_path,
        "shared_volume_path": shared_volume_path,
        "shared_marker_path": shared_marker_path,
    }


def download_model(
    control_plane: str,
    token: str,
    model_root: Path,
    item: dict[str, object],
    object_root: Path = MODEL_OBJECT_ROOT,
) -> None:
    folder = str(item["folder"])
    filename = str(item["filename"])
    expected_size = int(item["size_bytes"])
    expected_sha256 = str(item["sha256"])
    target = model_object_path(item, object_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".partial")

    if target.exists():
        if target.stat().st_size == expected_size and file_sha256(target) == expected_sha256:
            print(f"comfy-pod: model ready from cache — {folder}/{filename}", flush=True)
            link_model_alias(model_root, item, target)
            return
        target.unlink()

    if partial.exists() and partial.stat().st_size > expected_size:
        partial.unlink()
    offset = partial.stat().st_size if partial.exists() else 0
    download_url = urllib.parse.urljoin(
        control_plane.rstrip("/") + "/",
        str(item["download_path"]),
    )
    for attempt in range(1, 4):
        try:
            with request(download_url, token, offset) as response:
                status = getattr(response, "status", response.getcode())
                if offset > 0 and status != 206:
                    offset = 0
                    partial.unlink(missing_ok=True)
                mode = "ab" if offset > 0 and status == 206 else "wb"
                with partial.open(mode) as handle:
                    while chunk := response.read(CHUNK_BYTES):
                        handle.write(chunk)
            actual_size = partial.stat().st_size
            if actual_size != expected_size:
                raise BootstrapError(
                    f"model size mismatch for {folder}/{filename}: "
                    f"expected {expected_size}, received {actual_size}"
                )
            actual_sha256 = file_sha256(partial)
            if actual_sha256 != expected_sha256:
                partial.unlink(missing_ok=True)
                raise BootstrapError(
                    f"model SHA-256 mismatch for {folder}/{filename}"
                )
            os.replace(partial, target)
            print(
                f"comfy-pod: model prepared — {folder}/{filename} "
                f"({expected_size} bytes)",
                flush=True,
            )
            link_model_alias(model_root, item, target)
            return
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as error:
            if attempt == 3:
                raise BootstrapError(
                    f"model download failed for {folder}/{filename}: {error}"
                ) from error
            offset = partial.stat().st_size if partial.exists() else 0
            print(
                f"comfy-pod: retrying model download {folder}/{filename} "
                f"({attempt}/3) — {error}",
                flush=True,
            )
            time.sleep(attempt * 2)


def link_shared_model(
    shared_volume_root: Path,
    model_root: Path,
    item: dict[str, object],
) -> None:
    folder = str(item["folder"])
    filename = str(item["filename"])
    expected_size = int(item["size_bytes"])
    expected_sha256 = str(item["sha256"])
    relative_path = str(item["shared_volume_path"])
    relative_marker = str(item["shared_marker_path"])
    expected_relative_path = "models/" + canonical_model_relative_path(folder, filename)
    if relative_path != expected_relative_path:
        raise BootstrapError(f"shared model path does not match {folder}/{filename}")
    source = (shared_volume_root / relative_path).resolve(strict=True)
    marker = (shared_volume_root / relative_marker).resolve(strict=True)
    shared_root = shared_volume_root.resolve(strict=True)
    if not source.is_relative_to(shared_root) or not marker.is_relative_to(shared_root):
        raise BootstrapError("shared model path escaped the mounted volume")
    if source.stat().st_size != expected_size:
        raise BootstrapError(f"shared model size mismatch for {folder}/{filename}")
    try:
        with marker.open("r", encoding="utf-8") as handle:
            published = json.load(handle)
    except (OSError, json.JSONDecodeError) as error:
        raise BootstrapError(f"shared model marker is invalid for {folder}/{filename}") from error
    if not isinstance(published, dict) or (
        published.get("version") != 1
        or published.get("sha256") != expected_sha256
        or published.get("size_bytes") != expected_size
        or published.get("artifact_path") != relative_path
    ):
        raise BootstrapError(f"shared model marker does not match {folder}/{filename}")

    link_model_alias(model_root, item, source)
    print(f"comfy-pod: shared model ready — {folder}/{filename}", flush=True)


def write_extra_model_paths(
    config_path: Path,
    instance_root: Path,
    folders: set[str],
) -> None:
    entry: dict[str, object] = {"base_path": str(instance_root)}
    for folder in sorted(folders):
        entry[folder] = f"models/{folder}/"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = config_path.with_name(config_path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump({"comfy_instance": entry}, handle, ensure_ascii=True)
        handle.write("\n")
    os.replace(temporary, config_path)


def physical_byte_totals(items: list[dict[str, object]]) -> tuple[int, int, int]:
    """Return Container Disk, shared-volume, and combined physical bytes.

    R2-backed aliases with the same SHA-256 share one verified object under
    ``MODEL_OBJECT_ROOT``. Shared-volume items are addressed by canonical
    catalogue path and therefore remain distinct physical files even when
    their bytes happen to have the same digest.
    """

    r2_objects: dict[str, int] = {}
    all_objects: dict[str, int] = {}
    shared_volume_bytes = 0
    for item in items:
        size_bytes = int(item["size_bytes"])
        sha256 = str(item["sha256"])
        known_size = all_objects.get(sha256)
        if known_size is not None and known_size != size_bytes:
            raise BootstrapError("model manifest reuses a SHA-256 with conflicting sizes")
        all_objects[sha256] = size_bytes
        if item["source"] == "shared_volume":
            shared_volume_bytes += size_bytes
            continue
        r2_objects[sha256] = size_bytes
    r2_bytes = sum(r2_objects.values())
    return r2_bytes, shared_volume_bytes, r2_bytes + shared_volume_bytes


def bootstrap(
    instance_root: Path,
    config_path: Path,
    shared_volume_root: Path,
    model_object_root: Path = MODEL_OBJECT_ROOT,
) -> dict[str, int | str]:
    token = required_env("COMFY_POD_TOKEN")
    instance_id = required_env("COMFY_INSTANCE_ID")
    control_plane = required_env("COMFY_CONTROL_PLANE_URL")
    parsed_control_plane = urllib.parse.urlparse(control_plane)
    if parsed_control_plane.scheme != "https" and parsed_control_plane.hostname not in {
        "localhost",
        "127.0.0.1",
    }:
        raise BootstrapError("COMFY_CONTROL_PLANE_URL must use HTTPS")
    manifest = load_manifest(control_plane, instance_id, token)
    raw_items = manifest.get("items")
    if not isinstance(raw_items, list):
        raise BootstrapError("model manifest items are missing")
    items = [validate_item(value) for value in raw_items]
    expected_count = manifest.get("item_count")
    expected_r2 = manifest.get("r2_bytes")
    expected_shared = manifest.get("shared_volume_bytes")
    expected_total = manifest.get("total_bytes")
    r2_bytes, shared_volume_bytes, total_bytes = physical_byte_totals(items)
    if (
        expected_count != len(items)
        or expected_r2 != r2_bytes
        or expected_shared != shared_volume_bytes
        or expected_total != total_bytes
    ):
        raise BootstrapError("model manifest totals do not match its items")
    model_root = instance_root / "models"
    model_root.mkdir(parents=True, exist_ok=True)
    for item in items:
        if item["source"] == "shared_volume":
            link_shared_model(shared_volume_root, model_root, item)
        else:
            download_model(
                control_plane,
                token,
                model_root,
                item,
                model_object_root,
            )
    write_extra_model_paths(
        config_path,
        instance_root,
        {str(item["folder"]) for item in items},
    )
    return {
        "status": str(manifest.get("status", "ready")),
        "item_count": len(items),
        "total_bytes": total_bytes,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--instance-root", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--shared-volume-root", type=Path, required=True)
    parser.add_argument(
        "--model-object-root",
        type=Path,
        default=MODEL_OBJECT_ROOT,
    )
    args = parser.parse_args()
    if not integration_configured():
        print(
            "comfy-pod: control-plane credentials are not configured; "
            "model bootstrap is disabled",
            flush=True,
        )
        return
    result = bootstrap(
        args.instance_root,
        args.config,
        args.shared_volume_root,
        args.model_object_root,
    )
    print("comfy-pod: model bootstrap complete — " + json.dumps(result), flush=True)


if __name__ == "__main__":
    try:
        main()
    except BootstrapError as error:
        print(f"comfy-pod: model bootstrap failed — {error}", flush=True)
        raise SystemExit(1) from error
