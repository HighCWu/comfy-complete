#!/usr/bin/env python3
"""Fail-closed launcher for an already-materialized ComfyComplete runtime."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import sys
from typing import NoReturn

sys.path.insert(0, "/launcher-lib")

from runtime_manifest import RuntimeManifestError, validate_manifest  # noqa: E402


class LauncherError(RuntimeError):
    """The selected runtime cannot be launched safely."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_verified_manifest(
    manifest_path: Path,
    ready_path: Path,
    *,
    launcher_digest: str,
    launcher_abi: str,
    platform: str,
) -> dict[str, object]:
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = validate_manifest(json.loads(manifest_bytes))
        ready = json.loads(ready_path.read_bytes())
    except (OSError, json.JSONDecodeError, RuntimeManifestError) as error:
        raise LauncherError(f"runtime metadata is invalid: {error}") from error

    expected_ready = {
        "schema_version": 1,
        "runtime_digest": manifest["runtime_digest"],
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
    }
    if ready != expected_ready:
        raise LauncherError("runtime READY marker does not match the manifest")

    compatibility = manifest["compatibility"]
    expected = {
        "launcher_digest": launcher_digest,
        "launcher_abi": launcher_abi,
        "platform": platform,
    }
    for name, value in expected.items():
        if compatibility[name] != value:
            raise LauncherError(f"runtime compatibility mismatch: {name}")
    return manifest


def verify_runtime_tree(runtime_root: Path, manifest: dict[str, object], *, full: bool = False) -> None:
    """Verify launch-critical entries, or the complete tree in hydration mode.

    Walking hundreds of thousands of Network Volume entries on every paid Pod
    start would duplicate the hydrator's work and materially increase startup
    cost. READY binds the fully verified publication, so the normal launcher
    checks only the directories and executable files needed to enter ComfyUI.
    The single-writer volume hydrator must use ``full=True`` before publishing
    READY.
    """

    root = runtime_root.resolve(strict=True)
    file_tree = manifest["file_tree"]
    entries = file_tree["entries"]
    if full:
        selected_entries = entries
    else:
        entry_paths = {entry["path"] for entry in entries}
        critical_paths = {
            "app/comfyui",
            "opt/conda",
            "opt/conda/bin",
            "opt/conda/bin/python",
            manifest["entrypoint"]["path"],
        }
        critical_paths.update(
            argument
            for argument in manifest["entrypoint"]["argv"]
            if argument in entry_paths
        )
        selected_entries = [entry for entry in entries if entry["path"] in critical_paths]
        found = {entry["path"] for entry in selected_entries}
        missing = sorted(critical_paths - found)
        if missing:
            raise LauncherError("runtime manifest lacks launch-critical entries: " + ", ".join(missing))

    for entry in selected_entries:
        relative = entry["path"]
        path = runtime_root / relative
        try:
            metadata = path.lstat()
        except OSError as error:
            raise LauncherError(f"runtime entry is missing: {relative}") from error

        kind = entry["type"]
        mode = stat.S_IMODE(metadata.st_mode)
        if mode != entry["mode"]:
            raise LauncherError(f"runtime mode mismatch: {relative}")
        if kind == "directory":
            if not stat.S_ISDIR(metadata.st_mode):
                raise LauncherError(f"runtime type mismatch: {relative}")
        elif kind == "file":
            if not stat.S_ISREG(metadata.st_mode):
                raise LauncherError(f"runtime type mismatch: {relative}")
            if metadata.st_size != entry["size_bytes"]:
                raise LauncherError(f"runtime file digest mismatch: {relative}")
            if _sha256(path) != entry["sha256"]:
                raise LauncherError(f"runtime file digest mismatch: {relative}")
        elif kind == "symlink":
            if not stat.S_ISLNK(metadata.st_mode) or os.readlink(path) != entry["link_target"]:
                raise LauncherError(f"runtime symlink mismatch: {relative}")
            try:
                resolved = path.resolve(strict=True)
                resolved.relative_to(root)
            except (OSError, ValueError) as error:
                raise LauncherError(f"runtime symlink escapes or is broken: {relative}") from error
        else:
            raise LauncherError(f"unsupported runtime entry type: {relative}")


def install_compatibility_link(link: Path, target: Path) -> None:
    if not target.exists():
        raise LauncherError(f"runtime compatibility target is missing: {target}")
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.exists() and not link.is_symlink():
        raise LauncherError(f"refusing to replace a real compatibility path: {link}")
    if link.is_symlink() and Path(os.readlink(link)) == target:
        return
    temporary = link.with_name(f".{link.name}.runtime-{os.getpid()}")
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(target)
    os.replace(temporary, link)


def launch() -> NoReturn:
    configured_root = Path(os.environ.get("COMFY_RUNTIME_ROOT", "/runpod-volume/runtimes/current"))
    try:
        runtime_root = configured_root.resolve(strict=True)
    except OSError as error:
        raise LauncherError(f"runtime root is unavailable: {configured_root}") from error
    manifest_path = runtime_root / "manifest.json"
    ready_path = runtime_root / "READY.json"
    required = {
        "COMFY_EXPECTED_LAUNCHER_DIGEST": os.environ.get("COMFY_EXPECTED_LAUNCHER_DIGEST", ""),
        "COMFY_EXPECTED_LAUNCHER_ABI": os.environ.get("COMFY_EXPECTED_LAUNCHER_ABI", ""),
        "COMFY_EXPECTED_RUNTIME_PLATFORM": os.environ.get("COMFY_EXPECTED_RUNTIME_PLATFORM", ""),
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise LauncherError("missing launcher identity: " + ", ".join(missing))

    manifest = load_verified_manifest(
        manifest_path,
        ready_path,
        launcher_digest=required["COMFY_EXPECTED_LAUNCHER_DIGEST"],
        launcher_abi=required["COMFY_EXPECTED_LAUNCHER_ABI"],
        platform=required["COMFY_EXPECTED_RUNTIME_PLATFORM"],
    )
    verify_runtime_tree(runtime_root, manifest)
    install_compatibility_link(Path("/opt/conda"), runtime_root / "opt/conda")
    install_compatibility_link(Path("/app/comfyui"), runtime_root / "app/comfyui")
    install_compatibility_link(Path("/comfyui"), Path("/app/comfyui"))
    os.environ["PATH"] = "/opt/conda/bin:" + os.environ.get("PATH", "")
    os.chdir("/app/comfyui")
    os.execv("/start-pod.sh", ["/start-pod.sh"])


if __name__ == "__main__":
    try:
        launch()
    except LauncherError as error:
        print(f"comfy-runtime-launcher: {error}", file=sys.stderr, flush=True)
        raise SystemExit(1) from error
