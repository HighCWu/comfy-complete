#!/usr/bin/env python3
"""Export a selected final container file tree as a deterministic tar.zst.

This tool intentionally operates on a *materialized final filesystem tree*
(for example one produced with ``docker export``).  It never reads OCI image
history.  Only the two production base-image runtime roots and explicitly
named ``/app`` files may be selected; broad system roots such as ``/etc`` and
``/usr/local/cuda`` are rejected.

The archive contains relative paths only. File mtimes, owners, and archive
metadata are normalized for reproducibility while POSIX permission bits and
symbolic-link targets are retained. Hard links are intentionally represented
as ordinary files in v1: content is physically repeated, which may increase
bundle size but avoids aliasing and cross-root hard-link escapes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import posixpath
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from runtime_manifest import (
    RuntimeManifest,
    RuntimeManifestError,
    canonical_json,
    is_safe_relative_path,
    sha256_bytes,
    validate_manifest,
)


DEFAULT_TARGETS = ("/opt/conda", "/app/comfyui")
# These paths are mutable instance state, not an immutable shared runtime.
DEFAULT_EXCLUDES = (
    "/app/comfyui/output",
    "/app/comfyui/temp",
    "/app/comfyui/models/_xdgcache",
    "/app/comfyui/models/_xdgconfig",
    "/app/comfyui/models/_xdgdata",
)
ALLOWED_TARGETS = frozenset(DEFAULT_TARGETS)


class RuntimeExportError(RuntimeError):
    """Raised when a runtime export cannot be made safely."""


@dataclass(frozen=True)
class CollectedEntry:
    bundle_path: str
    source_path: Path
    kind: str
    mode: int
    size_bytes: int
    sha256: str | None = None
    link_target: str | None = None

    def manifest_entry(self) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "path": self.bundle_path,
            "type": self.kind,
            "mode": self.mode,
            "size_bytes": self.size_bytes,
        }
        if self.sha256 is not None:
            entry["sha256"] = self.sha256
        if self.link_target is not None:
            entry["link_target"] = self.link_target
        return entry


def _require_zstd() -> str:
    executable = shutil.which("zstd")
    if executable is None:
        raise RuntimeExportError(
            "zstd is required to create deterministic tar.zst archives; "
            "install the zstd CLI and retry"
        )
    return executable


def _safe_absolute_source(value: str, *, name: str) -> str:
    if not isinstance(value, str) or "\x00" in value or "\\" in value:
        raise RuntimeExportError(f"{name} contains an unsafe path")
    if not value.startswith("/") or not is_safe_relative_path(value[1:]):
        raise RuntimeExportError(f"{name} must be an absolute traversal-free POSIX path")
    return value.rstrip("/") or "/"


def _bundle_path(source: str) -> str:
    value = source.lstrip("/")
    if not is_safe_relative_path(value):
        raise RuntimeExportError(f"unsafe bundle path: {source!r}")
    return value


def _source_path(root: Path, source: str) -> Path:
    path = root / source.lstrip("/")
    try:
        path.resolve(strict=False).relative_to(root)
    except ValueError as error:
        raise RuntimeExportError(f"source path escapes root through a symlink: {source}") from error
    return path


def _normalise_exclusions(root: Path, values: Iterable[str]) -> set[str]:
    excluded: set[str] = set()
    for value in values:
        source = _safe_absolute_source(value, name="exclude")
        path = _source_path(root, source)
        excluded.add(_bundle_path(source))
        # A missing exclusion is a typo, not a reason to broaden the export.
        if not path.exists() and not path.is_symlink():
            raise RuntimeExportError(f"exclude path does not exist: {source}")
    return excluded


def _is_excluded(bundle_path: str, exclusions: set[str]) -> bool:
    return bundle_path in exclusions or any(bundle_path.startswith(item + "/") for item in exclusions)


def _read_file_digest(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                digest.update(chunk)
    except OSError as error:
        raise RuntimeExportError(f"cannot read runtime file {path}: {error}") from error
    return size, digest.hexdigest()


def _link_resolved_path(bundle_path: str, target: str) -> str:
    if not target or target.startswith("/") or "\x00" in target or "\\" in target:
        raise RuntimeExportError(f"unsafe symlink target for {bundle_path}: {target!r}")
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(bundle_path), target))
    if resolved == ".." or resolved.startswith("../") or not is_safe_relative_path(resolved):
        raise RuntimeExportError(f"symlink escapes runtime bundle: {bundle_path} -> {target}")
    return resolved


def _collect_one(path: Path, bundle_path: str, exclusions: set[str], result: dict[str, CollectedEntry]) -> None:
    if _is_excluded(bundle_path, exclusions):
        return
    try:
        info = path.lstat()
    except OSError as error:
        raise RuntimeExportError(f"cannot stat runtime path {path}: {error}") from error

    mode = stat.S_IMODE(info.st_mode)
    if stat.S_ISLNK(info.st_mode):
        try:
            target = os.readlink(path)
        except OSError as error:
            raise RuntimeExportError(f"cannot read symlink {path}: {error}") from error
        _link_resolved_path(bundle_path, target)
        entry = CollectedEntry(bundle_path, path, "symlink", mode, 0, link_target=target)
    elif stat.S_ISREG(info.st_mode):
        size, digest = _read_file_digest(path)
        if size != info.st_size:
            raise RuntimeExportError(f"file changed while hashing: {path}")
        entry = CollectedEntry(bundle_path, path, "file", mode, size, sha256=digest)
    elif stat.S_ISDIR(info.st_mode):
        entry = CollectedEntry(bundle_path, path, "directory", mode, 0)
    else:
        raise RuntimeExportError(f"unsupported special file in runtime export: {path}")

    previous = result.get(bundle_path)
    if previous is not None and previous != entry:
        raise RuntimeExportError(f"runtime path selected more than once: {bundle_path}")
    result[bundle_path] = entry

    if entry.kind != "directory":
        return
    try:
        children = sorted(path.iterdir(), key=lambda child: child.name)
    except OSError as error:
        raise RuntimeExportError(f"cannot enumerate runtime directory {path}: {error}") from error
    for child in children:
        child_bundle = f"{bundle_path}/{child.name}"
        if not is_safe_relative_path(child_bundle):
            raise RuntimeExportError(f"unsafe child path in runtime tree: {child_bundle}")
        _collect_one(child, child_bundle, exclusions, result)


def _validate_symlink_targets(entries: Sequence[CollectedEntry]) -> None:
    by_path = {entry.bundle_path: entry for entry in entries}
    for entry in entries:
        if entry.kind != "symlink" or entry.link_target is None:
            continue
        _resolve_symlink_chain(entry, by_path)


def _resolve_symlink_chain(
    entry: CollectedEntry,
    by_path: dict[str, CollectedEntry],
) -> CollectedEntry:
    """Resolve a selected symlink to its final selected tree entry."""

    current = entry
    seen: set[str] = set()
    while current.kind == "symlink":
        if current.bundle_path in seen:
            raise RuntimeExportError(f"symlink cycle in runtime tree at {entry.bundle_path}")
        seen.add(current.bundle_path)
        if current.link_target is None:
            raise RuntimeExportError(f"symlink metadata is incomplete: {current.bundle_path}")
        resolved = _link_resolved_path(current.bundle_path, current.link_target)
        next_entry = by_path.get(resolved)
        if next_entry is None:
            raise RuntimeExportError(
                f"symlink target is not part of the selected runtime tree: "
                f"{current.bundle_path} -> {current.link_target}"
            )
        current = next_entry
    return current


def collect_entries(
    source_root: Path,
    targets: Sequence[str],
    app_files: Sequence[str],
    exclusions: Sequence[str],
) -> list[CollectedEntry]:
    """Collect and hash the selected final tree without following symlinks."""

    try:
        root = source_root.resolve(strict=True)
    except OSError as error:
        raise RuntimeExportError(f"source root is unavailable: {source_root}: {error}") from error
    if not root.is_dir():
        raise RuntimeExportError(f"source root is not a directory: {source_root}")

    selected_targets: list[str] = []
    for raw_target in targets:
        target = _safe_absolute_source(raw_target, name="target")
        if target not in ALLOWED_TARGETS:
            raise RuntimeExportError(
                f"target {target} is not allowed; only /opt/conda and /app/comfyui "
                "may be exported as directories"
            )
        if target not in selected_targets:
            selected_targets.append(target)

    excluded = _normalise_exclusions(root, exclusions)
    result: dict[str, CollectedEntry] = {}
    for target in selected_targets:
        path = _source_path(root, target)
        if not path.is_dir() or path.is_symlink():
            raise RuntimeExportError(f"target must be a real directory: {target}")
        _collect_one(path, _bundle_path(target), excluded, result)

    for raw_file in app_files:
        app_file = _safe_absolute_source(raw_file, name="include-app")
        if not app_file.startswith("/app/"):
            raise RuntimeExportError("include-app paths must be under /app")
        path = _source_path(root, app_file)
        if not path.exists() and not path.is_symlink():
            raise RuntimeExportError(f"include-app path does not exist: {app_file}")
        if path.is_dir() and not path.is_symlink():
            raise RuntimeExportError(f"include-app accepts files, not directories: {app_file}")
        _collect_one(path, _bundle_path(app_file), excluded, result)

    entries = sorted(result.values(), key=lambda entry: entry.bundle_path)
    _validate_symlink_targets(entries)
    if not entries:
        raise RuntimeExportError("runtime export selection is empty")
    return entries


def _canonical_targets(targets: Sequence[str]) -> list[str]:
    """Return selected source roots in a stable, duplicate-free order."""

    return sorted(set(targets))


def _tar_info(entry: CollectedEntry) -> tarfile.TarInfo:
    info = tarfile.TarInfo(entry.bundle_path)
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = 0
    info.mode = entry.mode
    info.pax_headers = {}
    if entry.kind == "directory":
        info.type = tarfile.DIRTYPE
        info.size = 0
    elif entry.kind == "symlink":
        info.type = tarfile.SYMTYPE
        info.size = 0
        if entry.link_target is None:
            raise RuntimeExportError(f"symlink metadata is incomplete: {entry.bundle_path}")
        info.linkname = entry.link_target
    else:
        info.type = tarfile.REGTYPE
        info.size = entry.size_bytes
    return info


def _write_deterministic_tar(entries: Sequence[CollectedEntry], destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".partial")
    try:
        with temporary.open("wb") as raw_output, tarfile.open(
            fileobj=raw_output, mode="w", format=tarfile.PAX_FORMAT
        ) as archive:
            for entry in entries:
                current = entry.source_path.lstat()
                if stat.S_IMODE(current.st_mode) != entry.mode:
                    raise RuntimeExportError(f"runtime permissions changed during export: {entry.source_path}")
                info = _tar_info(entry)
                if entry.kind == "file":
                    size, digest = _read_file_digest(entry.source_path)
                    if size != entry.size_bytes or digest != entry.sha256:
                        raise RuntimeExportError(f"runtime file changed during export: {entry.source_path}")
                    with entry.source_path.open("rb") as input_file:
                        archive.addfile(info, input_file)
                elif entry.kind == "symlink":
                    current_target = os.readlink(entry.source_path)
                    if current_target != entry.link_target:
                        raise RuntimeExportError(f"runtime symlink changed during export: {entry.source_path}")
                    archive.addfile(info)
                else:
                    archive.addfile(info)
        os.replace(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _compress_zstd(tar_path: Path, archive_path: Path, zstd: str) -> None:
    temporary = archive_path.with_name(archive_path.name + ".partial")
    try:
        with tar_path.open("rb") as source, temporary.open("wb") as target:
            process = subprocess.run(
                [zstd, "-q", "-T1", "-19", "--no-progress", "-c"],
                stdin=source,
                stdout=target,
                stderr=subprocess.PIPE,
                check=False,
            )
        if process.returncode != 0:
            detail = process.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeExportError(f"zstd compression failed: {detail or process.returncode}")
        os.replace(temporary, archive_path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    finally:
        tar_path.unlink(missing_ok=True)


def _hash_file(path: Path) -> tuple[int, str]:
    return _read_file_digest(path)


def _verify_archive_entries(archive_path: Path, entries: Sequence[dict[str, Any]], zstd: str) -> None:
    """Compare every streamed tar member with the manifest file tree."""

    process = subprocess.Popen(
        [zstd, "-q", "-d", "-c", str(archive_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if process.stdout is None or process.stderr is None:
        process.kill()
        raise RuntimeExportError("could not open zstd verification stream")
    index = 0
    try:
        with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
            for member in archive:
                if index >= len(entries):
                    raise RuntimeExportError(f"runtime archive has an unexpected member: {member.name}")
                expected = entries[index]
                expected_path = expected["path"]
                if member.name != expected_path or not is_safe_relative_path(member.name):
                    raise RuntimeExportError(
                        f"runtime archive member does not match manifest: {member.name!r}"
                    )
                if stat.S_IMODE(member.mode) != expected["mode"] or member.uid != 0 or member.gid != 0 or member.mtime != 0:
                    raise RuntimeExportError(f"runtime archive metadata mismatch: {member.name}")
                kind = expected["type"]
                if kind == "file":
                    if not member.isfile() or member.size != expected["size_bytes"]:
                        raise RuntimeExportError(f"runtime archive file metadata mismatch: {member.name}")
                    fileobj = archive.extractfile(member)
                    if fileobj is None:
                        raise RuntimeExportError(f"runtime archive file payload is missing: {member.name}")
                    digest = hashlib.sha256()
                    size = 0
                    while True:
                        chunk = fileobj.read(1024 * 1024)
                        if not chunk:
                            break
                        size += len(chunk)
                        digest.update(chunk)
                    if size != expected["size_bytes"] or digest.hexdigest() != expected["sha256"]:
                        raise RuntimeExportError(f"runtime archive file digest mismatch: {member.name}")
                elif kind == "directory":
                    if not member.isdir() or member.size != 0:
                        raise RuntimeExportError(f"runtime archive directory metadata mismatch: {member.name}")
                elif kind == "symlink":
                    if not member.issym() or member.linkname != expected["link_target"]:
                        raise RuntimeExportError(f"runtime archive symlink metadata mismatch: {member.name}")
                else:
                    raise RuntimeExportError(f"unsupported manifest entry type: {kind}")
                index += 1
        stderr = process.stderr.read().decode("utf-8", errors="replace").strip()
        return_code = process.wait()
        if return_code != 0:
            raise RuntimeExportError(f"runtime archive decompression failed: {stderr or return_code}")
    except Exception:
        process.kill()
        process.wait()
        raise
    if index != len(entries):
        raise RuntimeExportError("runtime archive is missing manifest file-tree entries")


def _manifest(
    *,
    entries: Sequence[CollectedEntry],
    targets: Sequence[str],
    app_files: Sequence[str],
    exclusions: Sequence[str],
    archive_path: Path,
    runtime_version: str,
    source_image: str,
    source_image_digest: str,
    build_sha: str,
    platform: str,
    launcher_digest: str,
    launcher_abi: str,
    python_version: str | None,
    cuda_version: str | None,
    glibc_version: str | None,
    entrypoint: str,
    entrypoint_args: Sequence[str],
) -> RuntimeManifest:
    file_entries = [entry.manifest_entry() for entry in entries]
    total_bytes = sum(entry.size_bytes for entry in entries if entry.kind == "file")
    tree_sha256 = sha256_bytes(canonical_json(file_entries))
    archive_size, archive_sha256 = _hash_file(archive_path)
    runtime_digest = f"sha256:{tree_sha256}"
    compatibility: dict[str, str] = {
        "platform": platform,
        "launcher_digest": launcher_digest,
        "launcher_abi": launcher_abi,
    }
    for name, value in (
        ("python_version", python_version),
        ("cuda_version", cuda_version),
        ("glibc_version", glibc_version),
    ):
        if value is not None:
            compatibility[name] = value
    bundle_entrypoint = _bundle_path(entrypoint)
    argv = [bundle_entrypoint, *entrypoint_args]
    return {
        "schema_version": 1,
        "runtime_version": runtime_version,
        "runtime_digest": runtime_digest,
        "source": {
            "image": source_image,
            "image_digest": source_image_digest,
            "build_sha": build_sha,
        },
        "compatibility": compatibility,
        "entrypoint": {"path": bundle_entrypoint, "argv": argv},
        "targets": _canonical_targets(targets),
        "selection_policy": {
            "targets": _canonical_targets(targets),
            "include_app": sorted(set(app_files)),
            "excludes": sorted(set(exclusions)),
        },
        "file_tree": {
            "entry_count": len(file_entries),
            "total_bytes": total_bytes,
            "tree_sha256": tree_sha256,
            "entries": file_entries,
        },
        "archive": {
            "format": "tar.zst",
            "object_name": archive_path.name,
            "size_bytes": archive_size,
            "sha256": archive_sha256,
        },
    }


def _write_manifest(manifest: RuntimeManifest, path: Path) -> None:
    validated = validate_manifest(manifest)
    temporary = path.with_name(path.name + ".partial")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        temporary.write_bytes(canonical_json(validated) + b"\n")
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def export_runtime(
    *,
    source_root: Path,
    output_dir: Path,
    targets: Sequence[str],
    app_files: Sequence[str],
    exclusions: Sequence[str],
    runtime_version: str,
    source_image: str,
    source_image_digest: str,
    build_sha: str,
    platform: str,
    launcher_digest: str,
    launcher_abi: str,
    python_version: str | None,
    cuda_version: str | None,
    glibc_version: str | None,
    entrypoint: str,
    entrypoint_args: Sequence[str],
) -> tuple[Path, Path, RuntimeManifest]:
    zstd = _require_zstd()
    entries = collect_entries(source_root, targets, app_files, exclusions)
    entrypoint = _safe_absolute_source(entrypoint, name="entrypoint")
    entrypoint_bundle = _bundle_path(entrypoint)
    if not any(entry.bundle_path == entrypoint_bundle for entry in entries):
        raise RuntimeExportError("entrypoint must be inside a selected target or --include-app file")
    entrypoint_entry = next(entry for entry in entries if entry.bundle_path == entrypoint_bundle)
    if entrypoint_entry.kind not in ("file", "symlink"):
        raise RuntimeExportError("entrypoint must refer to a file or symlink")
    final_entrypoint = _resolve_symlink_chain(entrypoint_entry, {entry.bundle_path: entry for entry in entries})
    if final_entrypoint.kind != "file" or not (final_entrypoint.mode & 0o111):
        raise RuntimeExportError("entrypoint file is not executable")

    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="runtime-export-", dir=output_dir) as scratch:
        tar_path = Path(scratch) / "runtime.tar"
        compressed_path = Path(scratch) / "runtime.tar.zst"
        _write_deterministic_tar(entries, tar_path)
        _compress_zstd(tar_path, compressed_path, zstd)
        _, archive_sha256 = _hash_file(compressed_path)
        archive_name = f"sha256-{archive_sha256}.tar.zst"
        archive_path = output_dir / archive_name
        os.replace(compressed_path, archive_path)

    manifest = _manifest(
        entries=entries,
        targets=_canonical_targets(targets),
        app_files=sorted(set(_safe_absolute_source(path, name="include-app") for path in app_files)),
        exclusions=sorted(set(_safe_absolute_source(path, name="exclude") for path in exclusions)),
        archive_path=archive_path,
        runtime_version=runtime_version,
        source_image=source_image,
        source_image_digest=source_image_digest,
        build_sha=build_sha,
        platform=platform,
        launcher_digest=launcher_digest,
        launcher_abi=launcher_abi,
        python_version=python_version,
        cuda_version=cuda_version,
        glibc_version=glibc_version,
        entrypoint=entrypoint,
        entrypoint_args=entrypoint_args,
    )
    runtime_hex = manifest["runtime_digest"].removeprefix("sha256:")
    archive_hex = manifest["archive"]["sha256"]
    manifest_path = output_dir / f"sha256-{runtime_hex}-{archive_hex}.json"
    _write_manifest(manifest, manifest_path)
    return archive_path, manifest_path, manifest


def verify_export(archive_path: Path, manifest_path: Path) -> RuntimeManifest:
    """Verify manifest summaries, archive digest/size, and zstd integrity."""

    try:
        manifest = validate_manifest(json.loads(manifest_path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, RuntimeManifestError) as error:
        raise RuntimeExportError(f"runtime manifest verification failed: {error}") from error
    archive = manifest["archive"]
    if archive_path.name != archive["object_name"]:
        raise RuntimeExportError("archive filename does not match manifest")
    size, digest = _hash_file(archive_path)
    if size != archive["size_bytes"] or digest != archive["sha256"]:
        raise RuntimeExportError("runtime archive digest or size does not match manifest")
    zstd = _require_zstd()
    result = subprocess.run(
        [zstd, "-q", "-t", str(archive_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeExportError(f"runtime archive integrity check failed: {detail or result.returncode}")
    _verify_archive_entries(archive_path, manifest["file_tree"]["entries"], zstd)
    return manifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, help="materialized final container filesystem")
    parser.add_argument("--output-dir", type=Path, help="directory for archive and manifest")
    parser.add_argument("--target", action="append", dest="targets", help="runtime directory (repeat; /opt/conda or /app/comfyui)")
    parser.add_argument("--include-app", action="append", default=[], help="explicit app file allowlist entry (repeat; files under /app only)")
    parser.add_argument("--exclude", action="append", dest="exclusions", help="mutable source path to exclude (repeat)")
    parser.add_argument("--runtime-version")
    parser.add_argument("--source-image")
    parser.add_argument("--source-image-digest")
    parser.add_argument("--build-sha", help="40-character source/build git SHA")
    parser.add_argument("--platform", default="linux/amd64")
    parser.add_argument("--launcher-digest")
    parser.add_argument("--launcher-abi")
    parser.add_argument("--python-version")
    parser.add_argument("--cuda-version")
    parser.add_argument("--glibc-version")
    parser.add_argument("--entrypoint", help="absolute source path selected into the bundle")
    parser.add_argument("--entrypoint-arg", action="append", default=[], help="argument after the entrypoint (repeat)")
    parser.add_argument("--verify", nargs=2, metavar=("ARCHIVE", "MANIFEST"), help="verify an existing export and exit")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.verify:
            verify_export(Path(args.verify[0]), Path(args.verify[1]))
            print("runtime export verified")
            return 0
        required = {
            "--source-root": args.source_root,
            "--output-dir": args.output_dir,
            "--runtime-version": args.runtime_version,
            "--source-image": args.source_image,
            "--source-image-digest": args.source_image_digest,
            "--build-sha": args.build_sha,
            "--launcher-digest": args.launcher_digest,
            "--launcher-abi": args.launcher_abi,
            "--entrypoint": args.entrypoint,
        }
        missing = [name for name, value in required.items() if value is None]
        if missing:
            raise RuntimeExportError("missing required export options: " + ", ".join(missing))
        targets = args.targets or list(DEFAULT_TARGETS)
        exclusions = args.exclusions or list(DEFAULT_EXCLUDES)
        archive, manifest_path, manifest = export_runtime(
            source_root=args.source_root,
            output_dir=args.output_dir,
            targets=targets,
            app_files=args.include_app,
            exclusions=exclusions,
            runtime_version=args.runtime_version,
            source_image=args.source_image,
            source_image_digest=args.source_image_digest,
            build_sha=args.build_sha,
            platform=args.platform,
            launcher_digest=args.launcher_digest,
            launcher_abi=args.launcher_abi,
            python_version=args.python_version,
            cuda_version=args.cuda_version,
            glibc_version=args.glibc_version,
            entrypoint=args.entrypoint,
            entrypoint_args=args.entrypoint_arg,
        )
        verify_export(archive, manifest_path)
        print(json.dumps({"archive": str(archive), "manifest": str(manifest_path), "runtime_digest": manifest["runtime_digest"]}, sort_keys=True))
        return 0
    except (RuntimeExportError, RuntimeManifestError, OSError) as error:
        print(f"runtime export failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
