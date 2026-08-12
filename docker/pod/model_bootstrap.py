"""Prepare the exact model set bound to the active Laimon Pod start quote.

The control plane authenticates this container with the lease-derived Pod
token and streams private R2 objects without exposing R2 credentials or keys.
Downloads are resumable, content-verified, and atomically published into the
instance's managed model directory before ComfyUI starts.
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


TOKEN_HEADER = "X-Laimon-Pod-Token"
SHA256_HEX = re.compile(r"^[a-f0-9]{64}$")
SAFE_FOLDER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
SAFE_SHARED_PATH = re.compile(r"^shared/models/objects/[a-f0-9]{2}/[a-f0-9]{64}/[A-Za-z0-9._-]+$")
CHUNK_BYTES = 4 * 1024 * 1024
MANIFEST_WAIT_SECONDS = 300


class BootstrapError(RuntimeError):
    """Terminal model-bootstrap failure."""


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise BootstrapError(f"{name} is required")
    return value


def safe_filename(value: str) -> bool:
    return (
        bool(value)
        and len(value) <= 255
        and value not in {".", ".."}
        and "/" not in value
        and "\\" not in value
        and "\x00" not in value
    )


def request(
    url: str,
    token: str,
    range_start: int | None = None,
) -> urllib.response.addinfourl:
    headers = {
        TOKEN_HEADER: token,
        "Accept": "application/json" if range_start is None else "application/octet-stream",
        "User-Agent": "laimon-pod-model-bootstrap/1",
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
        print(f"laimon-pod: waiting for model manifest — {last_error}", flush=True)
        time.sleep(2)
    raise BootstrapError(f"model manifest did not become ready: {last_error}")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


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
        or not SAFE_FOLDER.fullmatch(folder)
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
        or not SAFE_SHARED_PATH.fullmatch(shared_volume_path)
        or not isinstance(shared_marker_path, str)
        or not SAFE_SHARED_PATH.fullmatch(shared_marker_path)
        or shared_marker_path != shared_volume_path + ".laimon.json"
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
) -> None:
    folder = str(item["folder"])
    filename = str(item["filename"])
    expected_size = int(item["size_bytes"])
    expected_sha256 = str(item["sha256"])
    target_dir = model_root / folder
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / filename
    partial = target.with_name(target.name + ".partial")

    if target.exists():
        if target.stat().st_size == expected_size and file_sha256(target) == expected_sha256:
            print(f"laimon-pod: model ready from cache — {folder}/{filename}", flush=True)
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
                f"laimon-pod: model prepared — {folder}/{filename} "
                f"({expected_size} bytes)",
                flush=True,
            )
            return
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as error:
            if attempt == 3:
                raise BootstrapError(
                    f"model download failed for {folder}/{filename}: {error}"
                ) from error
            offset = partial.stat().st_size if partial.exists() else 0
            print(
                f"laimon-pod: retrying model download {folder}/{filename} "
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

    target_dir = model_root / folder
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / filename
    if target.is_symlink() and target.resolve(strict=True) == source:
        return
    if target.exists() or target.is_symlink():
        raise BootstrapError(f"shared model target collision for {folder}/{filename}")
    temporary = target.with_name(f".{target.name}.laimon-shared-link")
    temporary.unlink(missing_ok=True)
    try:
        temporary.symlink_to(source)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    print(f"laimon-pod: shared model ready — {folder}/{filename}", flush=True)


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
        json.dump({"laimon_instance": entry}, handle, ensure_ascii=True)
        handle.write("\n")
    os.replace(temporary, config_path)


def publish_comfy_model_links(
    comfy_model_root: Path,
    instance_model_root: Path,
    items: list[dict[str, object]],
) -> None:
    """Expose verified instance models through ComfyUI's default folders.

    The instance directory remains the authoritative disposable cache.  These
    links are container-local compatibility entries: some ComfyUI loaders take
    their initial filename list from the default model directory even when an
    equivalent extra search path is configured.
    """
    for item in items:
        folder = str(item["folder"])
        filename = str(item["filename"])
        target = (instance_model_root / folder / filename).resolve(strict=True)
        link_dir = comfy_model_root / folder
        link_dir.mkdir(parents=True, exist_ok=True)
        link = link_dir / filename
        if link.is_symlink():
            try:
                if link.resolve(strict=True) == target:
                    continue
            except FileNotFoundError:
                pass
            raise BootstrapError(
                f"ComfyUI model link collision for {folder}/{filename}"
            )
        if link.exists():
            raise BootstrapError(
                f"ComfyUI model path collision for {folder}/{filename}"
            )
        temporary = link.with_name(f".{link.name}.laimon-link")
        temporary.unlink(missing_ok=True)
        try:
            temporary.symlink_to(target)
            os.replace(temporary, link)
        finally:
            temporary.unlink(missing_ok=True)


def bootstrap(
    instance_root: Path,
    config_path: Path,
    comfy_model_root: Path,
    shared_volume_root: Path,
) -> dict[str, int | str]:
    token = required_env("LAIMON_POD_TOKEN")
    instance_id = required_env("LAIMON_INSTANCE_ID")
    control_plane = required_env("LAIMON_CONTROL_PLANE_URL")
    parsed_control_plane = urllib.parse.urlparse(control_plane)
    if parsed_control_plane.scheme != "https" and parsed_control_plane.hostname not in {
        "localhost",
        "127.0.0.1",
    }:
        raise BootstrapError("LAIMON_CONTROL_PLANE_URL must use HTTPS")
    manifest = load_manifest(control_plane, instance_id, token)
    raw_items = manifest.get("items")
    if not isinstance(raw_items, list):
        raise BootstrapError("model manifest items are missing")
    items = [validate_item(value) for value in raw_items]
    expected_count = manifest.get("item_count")
    expected_total = manifest.get("total_bytes")
    if expected_count != len(items) or expected_total != sum(
        int(item["size_bytes"]) for item in items
    ):
        raise BootstrapError("model manifest totals do not match its items")
    model_root = instance_root / "models"
    model_root.mkdir(parents=True, exist_ok=True)
    for item in items:
        if item["source"] == "shared_volume":
            link_shared_model(shared_volume_root, model_root, item)
        else:
            download_model(control_plane, token, model_root, item)
    write_extra_model_paths(
        config_path,
        instance_root,
        {str(item["folder"]) for item in items},
    )
    publish_comfy_model_links(comfy_model_root, model_root, items)
    return {
        "status": str(manifest.get("status", "ready")),
        "item_count": len(items),
        "total_bytes": sum(int(item["size_bytes"]) for item in items),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--instance-root", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--comfy-model-root", type=Path, required=True)
    parser.add_argument("--shared-volume-root", type=Path, required=True)
    args = parser.parse_args()
    result = bootstrap(
        args.instance_root,
        args.config,
        args.comfy_model_root,
        args.shared_volume_root,
    )
    print("laimon-pod: model bootstrap complete — " + json.dumps(result), flush=True)


if __name__ == "__main__":
    try:
        main()
    except BootstrapError as error:
        print(f"laimon-pod: model bootstrap failed — {error}", flush=True)
        raise SystemExit(1) from error
