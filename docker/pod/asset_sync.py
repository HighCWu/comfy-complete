"""Mirror disposable Pod inputs and outputs to the Compute R2 control plane.

The Pod never receives R2 credentials or object keys. Every request is bound
to the active runtime lease through ``X-Comfy-Pod-Token``. Inputs are restored
before ComfyUI starts; a small watcher then mirrors stable input/output files
through bounded R2 multipart requests.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


TOKEN_HEADER = "X-Comfy-Pod-Token"
PART_BYTES = 32 * 1024 * 1024
MAX_FILE_BYTES = 5 * 1024 * 1024 * 1024
SCAN_SECONDS = 2
INPUT_STABLE_SECONDS = 2
OUTPUT_STABLE_SECONDS = 10
STATE_FILENAME = ".asset-sync.json"
SHA256_HEX = frozenset("0123456789abcdef")
STOP = False


class AssetSyncError(RuntimeError):
    """A bounded, retryable asset synchronization failure."""


def integration_configured() -> bool:
    """Return whether the optional control-plane integration is configured."""
    names = ("COMFY_CONTROL_PLANE_URL", "COMFY_INSTANCE_ID", "COMFY_POD_TOKEN")
    configured = [bool(os.environ.get(name, "").strip()) for name in names]
    if any(configured) and not all(configured):
        raise AssetSyncError("control-plane variables must be configured together")
    return all(configured)


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise AssetSyncError(f"{name} is required")
    return value


def control_plane_origin() -> str:
    value = required_env("COMFY_CONTROL_PLANE_URL")
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1"}:
        raise AssetSyncError("COMFY_CONTROL_PLANE_URL must use HTTPS")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise AssetSyncError("COMFY_CONTROL_PLANE_URL must be an origin")
    return value.rstrip("/")


def safe_relative_path(value: str) -> str:
    if not value or len(value) > 1024 or value.startswith("/") or "\\" in value or "\0" in value:
        raise AssetSyncError("unsafe asset relative path")
    parts = value.split("/")
    if any(not part or part in {".", ".."} or len(part) > 255 for part in parts):
        raise AssetSyncError("unsafe asset relative path")
    return "/".join(parts)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(4 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def request(
    url: str,
    token: str,
    *,
    method: str = "GET",
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 120,
) -> urllib.response.addinfourl:
    merged = {
        TOKEN_HEADER: token,
        "User-Agent": "comfy-pod-asset-sync/1",
        **(headers or {}),
    }
    return urllib.request.urlopen(
        urllib.request.Request(url, data=data, headers=merged, method=method),
        timeout=timeout,
    )


def api_url(origin: str, path: str, instance_id: str) -> str:
    separator = "&" if "?" in path else "?"
    return (
        urllib.parse.urljoin(origin + "/", path.lstrip("/"))
        + separator
        + "instance_id="
        + urllib.parse.quote(instance_id, safe="")
    )


def json_request(
    origin: str,
    path: str,
    instance_id: str,
    token: str,
    payload: object | None = None,
    method: str = "GET",
) -> Any:
    data = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    with request(
        api_url(origin, path, instance_id),
        token,
        method=method,
        data=data,
        headers=headers,
    ) as response:
        return json.load(response)


def restore_inputs(instance_root: Path) -> None:
    origin = control_plane_origin()
    instance_id = required_env("COMFY_INSTANCE_ID")
    token = required_env("COMFY_POD_TOKEN")
    manifest = json_request(
        origin,
        "/api/internal/pod-assets/manifest",
        instance_id,
        token,
    )
    if not isinstance(manifest, dict) or manifest.get("version") != 1:
        raise AssetSyncError("input manifest has an unsupported shape")
    if manifest.get("instance_id") != instance_id or not isinstance(manifest.get("items"), list):
        raise AssetSyncError("input manifest binding is invalid")
    input_root = instance_root / "input"
    input_root.mkdir(parents=True, exist_ok=True)
    for raw in manifest["items"]:
        if not isinstance(raw, dict):
            raise AssetSyncError("input manifest item is invalid")
        relative = safe_relative_path(str(raw.get("relative_path", "")))
        expected_hash = str(raw.get("sha256", "")).lower()
        expected_size = raw.get("size_bytes")
        download_path = raw.get("download_path")
        if (
            len(expected_hash) != 64
            or any(char not in SHA256_HEX for char in expected_hash)
            or not isinstance(expected_size, int)
            or isinstance(expected_size, bool)
            or expected_size <= 0
            or not isinstance(download_path, str)
            or not download_path.startswith("/api/internal/pod-assets/")
        ):
            raise AssetSyncError("input manifest item metadata is invalid")
        target = input_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.stat().st_size == expected_size:
            if sha256_file(target) == expected_hash:
                continue
            target.unlink()
        partial = target.with_name(target.name + ".partial")
        with request(api_url(origin, download_path, instance_id), token) as response:
            with partial.open("wb") as handle:
                while chunk := response.read(4 * 1024 * 1024):
                    handle.write(chunk)
        if partial.stat().st_size != expected_size or sha256_file(partial) != expected_hash:
            partial.unlink(missing_ok=True)
            raise AssetSyncError(f"restored input failed verification: {relative}")
        os.replace(partial, target)
        print(f"comfy-pod: restored input — {relative}", flush=True)


def content_type(path: Path) -> str:
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def upload_file(instance_root: Path, kind: str, path: Path) -> dict[str, object]:
    origin = control_plane_origin()
    instance_id = required_env("COMFY_INSTANCE_ID")
    token = required_env("COMFY_POD_TOKEN")
    root = instance_root / kind
    relative = safe_relative_path(path.relative_to(root).as_posix())
    size = path.stat().st_size
    if size <= 0 or size > MAX_FILE_BYTES:
        raise AssetSyncError(f"asset size is outside the supported range: {relative}")
    digest = sha256_file(path)
    begun = json_request(
        origin,
        "/api/internal/pod-assets/uploads",
        instance_id,
        token,
        {
            "kind": kind,
            "relative_path": relative,
            "sha256": digest,
            "size_bytes": size,
            "content_type": content_type(path),
        },
        "POST",
    )
    if isinstance(begun, dict) and begun.get("status") == "ready":
        return {"sha256": digest, "size": size, "mtime_ns": path.stat().st_mtime_ns}
    if not isinstance(begun, dict) or not isinstance(begun.get("upload_id"), str):
        raise AssetSyncError("asset upload did not return an upload id")
    upload_id = begun["upload_id"]
    parts: list[dict[str, object]] = []
    try:
        with path.open("rb") as handle:
            part_number = 1
            while chunk := handle.read(PART_BYTES):
                part_path = (
                    "/api/internal/pod-assets/uploads/"
                    + urllib.parse.quote(upload_id, safe="")
                    + f"/parts/{part_number}"
                )
                with request(
                    api_url(origin, part_path, instance_id),
                    token,
                    method="PUT",
                    data=chunk,
                    headers={"Content-Type": "application/octet-stream"},
                    timeout=300,
                ) as response:
                    uploaded = json.load(response)
                if not isinstance(uploaded, dict) or not isinstance(uploaded.get("etag"), str):
                    raise AssetSyncError("asset part response is invalid")
                parts.append({
                    "part_number": int(uploaded.get("part_number", part_number)),
                    "etag": uploaded["etag"],
                })
                part_number += 1
        complete_path = (
            "/api/internal/pod-assets/uploads/"
            + urllib.parse.quote(upload_id, safe="")
            + "/complete"
        )
        completed = json_request(
            origin,
            complete_path,
            instance_id,
            token,
            {"parts": parts},
            "POST",
        )
        if not isinstance(completed, dict) or completed.get("status") != "ready":
            raise AssetSyncError("asset upload did not complete")
    except Exception:
        try:
            request(
                api_url(
                    origin,
                    "/api/internal/pod-assets/uploads/"
                    + urllib.parse.quote(upload_id, safe=""),
                    instance_id,
                ),
                token,
                method="DELETE",
            ).close()
        except Exception:
            pass
        raise
    print(f"comfy-pod: mirrored {kind} asset — {relative} ({size} bytes)", flush=True)
    stat = path.stat()
    return {"sha256": digest, "size": size, "mtime_ns": stat.st_mtime_ns}


def load_state(path: Path) -> dict[str, dict[str, object]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return value if isinstance(value, dict) else {}


def save_state(path: Path, state: dict[str, dict[str, object]]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(state, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def candidate_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file()
        and path.name != STATE_FILENAME
        and not path.name.endswith((".partial", ".tmp"))
    )


def watch(instance_root: Path) -> None:
    state_path = instance_root / STATE_FILENAME
    state = load_state(state_path)
    observed: dict[str, tuple[int, int, float]] = {}
    while not STOP:
        changed = False
        now = time.monotonic()
        for kind in ("input", "output"):
            root = instance_root / kind
            stable_seconds = INPUT_STABLE_SECONDS if kind == "input" else OUTPUT_STABLE_SECONDS
            for path in candidate_files(root):
                try:
                    stat = path.stat()
                    key = f"{kind}/{path.relative_to(root).as_posix()}"
                    stored = state.get(key)
                    if stored and stored.get("size") == stat.st_size and stored.get("mtime_ns") == stat.st_mtime_ns:
                        continue
                    signature = (stat.st_size, stat.st_mtime_ns)
                    previous = observed.get(key)
                    if not previous or previous[:2] != signature:
                        observed[key] = (*signature, now)
                        continue
                    if now - previous[2] < stable_seconds:
                        continue
                    state[key] = upload_file(instance_root, kind, path)
                    observed.pop(key, None)
                    changed = True
                except (AssetSyncError, OSError, urllib.error.URLError, urllib.error.HTTPError) as error:
                    print(f"comfy-pod: asset sync retry pending — {error}", file=sys.stderr, flush=True)
        if changed:
            save_state(state_path, state)
        time.sleep(SCAN_SECONDS)


def stop_handler(_signum: int, _frame: object) -> None:
    global STOP
    STOP = True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["restore", "watch"])
    parser.add_argument("--instance-root", type=Path, required=True)
    args = parser.parse_args()
    args.instance_root.mkdir(parents=True, exist_ok=True)
    if not integration_configured():
        print(
            "comfy-pod: control-plane credentials are not configured; "
            "asset synchronization is disabled",
            file=sys.stderr,
            flush=True,
        )
        return
    if args.command == "restore":
        restore_inputs(args.instance_root)
        return
    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)
    watch(args.instance_root)


if __name__ == "__main__":
    try:
        main()
    except AssetSyncError as error:
        print(f"comfy-pod: asset sync failed — {error}", file=sys.stderr, flush=True)
        raise SystemExit(1) from error
