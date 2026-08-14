#!/usr/bin/env python3
"""Materialize a verified runtime archive into a local Network Volume.

This helper is intentionally provider-neutral.  It accepts a downloaded,
content-addressed ``tar.zst`` and the matching runtime manifest, expands the
archive through the ``zstd`` command line tool into a private staging
directory, and publishes one immutable generation.  ``current`` is replaced
only after the generation has passed a second complete verification.

The only mutable state owned by this tool is ``<volume>/runtimes/.staging``,
``<volume>/runtimes/.materialize.lock``, and the ``current`` symlink.  Existing
generations are never removed or overwritten.  There is no object-store,
RunPod, or network code here; the archive and manifest must already be local.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import posixpath
import shutil
import stat
import subprocess
import tarfile
import tempfile
import uuid
from typing import Any, Iterator, Mapping, Sequence

from runtime_manifest import (
    RuntimeManifest,
    RuntimeManifestError,
    canonical_json,
    is_safe_relative_path,
    validate_manifest,
)
from runtime_ready import RuntimeReadyError, build_ready_marker


MIB = 1024 * 1024
CHUNK_BYTES = 8 * MIB
MAX_MANIFEST_BYTES = 128 * MIB
MAX_READY_BYTES = 64 * 1024
MAX_ENTRY_COUNT = 1_000_000
RUNTIME_DIRECTORY = "runtimes"
STAGING_DIRECTORY = ".staging"
LOCK_NAME = ".materialize.lock"
CURRENT_NAME = "current"
MANIFEST_NAME = "manifest.json"
READY_NAME = "READY.json"


class RuntimeMaterializerError(RuntimeError):
    """A fail-closed local materialization error.

    ``detail`` is useful to unit tests and callers using the Python API.  The
    command-line entrypoint deliberately emits only ``code`` so a local
    filesystem path can never appear in its bounded JSON output.
    """

    def __init__(self, code: str, detail: str | None = None) -> None:
        self.code = code
        self.reason = code
        self.detail = detail or code
        super().__init__(f"{code}: {self.detail}")


def _error(code: str, detail: str | None = None) -> RuntimeMaterializerError:
    return RuntimeMaterializerError(code, detail)


def _lexists(path: Path) -> bool:
    """Return whether *path* exists without following a final symlink."""

    return os.path.lexists(path)


def _is_real_directory(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return stat.S_ISDIR(metadata.st_mode)


def _ensure_real_directory(path: Path, *, create: bool = False) -> None:
    if _lexists(path):
        if not _is_real_directory(path):
            raise _error("unsafe_volume_root")
        return
    if not create:
        raise _error("missing_runtime_root")
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o755)
    except OSError as error:
        raise _error("volume_io") from error
    if not _is_real_directory(path):
        raise _error("unsafe_volume_root")


def _read_regular_file(path: Path, *, max_bytes: int | None = None) -> bytes:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise _error("input_unavailable") from error
    if not stat.S_ISREG(metadata.st_mode):
        raise _error("input_not_regular")
    if max_bytes is not None and metadata.st_size > max_bytes:
        raise _error("input_too_large")
    try:
        return path.read_bytes()
    except OSError as error:
        raise _error("input_unavailable") from error


def _hash_regular_file(path: Path) -> tuple[int, str]:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise _error("archive_unavailable") from error
    if not stat.S_ISREG(metadata.st_mode):
        raise _error("archive_not_regular")
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(CHUNK_BYTES)
                if not chunk:
                    break
                size += len(chunk)
                digest.update(chunk)
    except OSError as error:
        raise _error("archive_unavailable") from error
    return size, digest.hexdigest()


def _read_manifest(path: Path) -> tuple[bytes, RuntimeManifest]:
    payload = _read_regular_file(path, max_bytes=MAX_MANIFEST_BYTES)
    if not payload:
        raise _error("manifest_invalid")
    try:
        value = json.loads(payload.decode("utf-8"))
        manifest = validate_manifest(value)
    except (UnicodeDecodeError, json.JSONDecodeError, RuntimeManifestError) as error:
        raise _error("manifest_invalid", "runtime manifest failed validation") from error
    entries = manifest["file_tree"]["entries"]
    if len(entries) > MAX_ENTRY_COUNT:
        raise _error("manifest_too_large")
    return payload, manifest


def _validate_archive_input(archive_path: Path, manifest: RuntimeManifest) -> tuple[int, str]:
    archive = manifest["archive"]
    if archive_path.name != archive["object_name"]:
        raise _error("archive_name_mismatch")
    size, digest = _hash_regular_file(archive_path)
    if size != archive["size_bytes"] or digest != archive["sha256"]:
        raise _error("archive_digest_mismatch")
    return size, digest


def _require_zstd() -> str:
    executable = shutil.which("zstd")
    if executable is None:
        raise _error("zstd_unavailable")
    return executable


@contextmanager
def _writer_lock(path: Path) -> Iterator[None]:
    """Hold one blocking process lock for all publication operations."""

    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as error:
        raise _error("lock_unavailable") from error
    try:
        try:
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        except OSError as error:
            raise _error("lock_unavailable") from error
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError as error:
        raise _error("volume_io") from error
    try:
        os.fsync(descriptor)
    except OSError as error:
        raise _error("volume_io") from error
    finally:
        os.close(descriptor)


def _fsync_tree_directories(root: Path) -> None:
    """Persist directory entries below a staged generation bottom-up."""

    directories: list[Path] = []
    stack = [root]
    while stack:
        directory = stack.pop()
        directories.append(directory)
        try:
            with os.scandir(directory) as iterator:
                children = list(iterator)
        except OSError as error:
            raise _error("staging_io") from error
        for child in children:
            try:
                metadata = child.stat(follow_symlinks=False)
            except OSError as error:
                raise _error("staging_io") from error
            if stat.S_ISDIR(metadata.st_mode):
                stack.append(directory / child.name)
    for directory in reversed(directories):
        _fsync_directory(directory)


def _atomic_write(path: Path, payload: bytes, *, mode: int) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".partial",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            os.fchmod(handle.fileno(), mode)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        _fsync_directory(path.parent)
    except RuntimeMaterializerError:
        raise
    except OSError as error:
        raise _error("publication_io") from error
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _remove_tree(path: Path) -> None:
    """Remove only a private staging tree, never following symlinks."""

    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    except OSError:
        return
    if not stat.S_ISDIR(metadata.st_mode):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return
    try:
        with os.scandir(path) as iterator:
            children = list(iterator)
    except OSError:
        children = []
    for child in children:
        _remove_tree(path / child.name)
    try:
        path.rmdir()
    except OSError:
        pass


def _safe_member_path(name: object) -> str:
    if not isinstance(name, str) or not is_safe_relative_path(name):
        raise _error("unsafe_member", "archive member path is unsafe")
    return name


def _safe_link_target(path: str, target: object) -> str:
    if not isinstance(target, str) or not target or target.startswith("/"):
        raise _error("unsafe_symlink", "archive symlink target is unsafe")
    if "\\" in target or "\x00" in target:
        raise _error("unsafe_symlink", "archive symlink target is unsafe")
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
    if resolved in (".", "..") or resolved.startswith("../") or not is_safe_relative_path(resolved):
        raise _error("unsafe_symlink", "archive symlink target escapes runtime")
    return target


def _expected_entries(manifest: RuntimeManifest) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for entry in manifest["file_tree"]["entries"]:
        path = entry["path"]
        if path in (MANIFEST_NAME, READY_NAME, CURRENT_NAME):
            raise _error("manifest_invalid", "reserved runtime metadata path is selected")
        result[path] = entry
    return result


def _implicit_directories(expected: Mapping[str, Mapping[str, Any]]) -> set[str]:
    implicit: set[str] = set()
    for path in expected:
        parent = posixpath.dirname(path)
        while parent and parent != ".":
            if parent not in expected:
                implicit.add(parent)
            parent = posixpath.dirname(parent)
    return implicit


def _ensure_parents(root: Path, relative: str, expected: Mapping[str, Mapping[str, Any]]) -> None:
    current = root
    parts = relative.split("/")[:-1]
    prefix_parts: list[str] = []
    for part in parts:
        prefix_parts.append(part)
        prefix = "/".join(prefix_parts)
        current = current / part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            declared = expected.get(prefix)
            if declared is not None and declared["type"] != "directory":
                raise _error("tree_conflict", "a runtime path parent is not a directory")
            try:
                current.mkdir(mode=0o700)
            except OSError as error:
                raise _error("staging_io") from error
            continue
        except OSError as error:
            raise _error("staging_io") from error
        if not stat.S_ISDIR(metadata.st_mode):
            raise _error("tree_conflict", "a runtime path parent is not a directory")


def _check_tar_metadata(member: tarfile.TarInfo, expected: Mapping[str, Any]) -> None:
    if (
        member.uid != 0
        or member.gid != 0
        or member.mtime != 0
        or member.devmajor != 0
        or member.devminor != 0
    ):
        raise _error("member_metadata_mismatch", "archive ownership or timestamp differs")
    if member.uname not in ("", None) or member.gname not in ("", None):
        raise _error("member_metadata_mismatch", "archive owner names differ")
    if stat.S_IMODE(member.mode) != expected["mode"]:
        raise _error("member_mode_mismatch", "archive mode differs from manifest")
    # The exporter uses PAX format for long names.  ``path`` and ``linkpath``
    # are structural aliases already reflected by TarInfo; arbitrary PAX
    # metadata would be an unbound metadata channel and is rejected.
    if any(key not in {"path", "linkpath"} for key in member.pax_headers):
        raise _error("member_metadata_mismatch", "archive contains unbound metadata")


def _open_new_file(path: Path, mode: int) -> int:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, mode)
        os.fchmod(descriptor, mode)
        return descriptor
    except OSError as error:
        raise _error("staging_io") from error


def _extract_file(fileobj: Any, path: Path, expected: Mapping[str, Any]) -> None:
    descriptor = _open_new_file(path, expected["mode"])
    digest = hashlib.sha256()
    size = 0
    try:
        while True:
            chunk = fileobj.read(CHUNK_BYTES)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
            try:
                view = memoryview(chunk)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise OSError("short file write")
                    view = view[written:]
            except OSError as error:
                raise _error("staging_io") from error
        if size != expected["size_bytes"] or digest.hexdigest() != expected["sha256"]:
            raise _error("member_digest_mismatch", "archive file content differs from manifest")
        try:
            os.fsync(descriptor)
        except OSError as error:
            raise _error("staging_io") from error
    finally:
        os.close(descriptor)


def _extract_member(
    member: tarfile.TarInfo,
    archive: tarfile.TarFile,
    staging: Path,
    expected: Mapping[str, Mapping[str, Any]],
    seen: set[str],
) -> None:
    path_name = _safe_member_path(member.name)
    declared = expected.get(path_name)
    if declared is None:
        raise _error("unexpected_member", "archive contains a member absent from manifest")
    if path_name in seen:
        raise _error("duplicate_member", "archive contains a duplicate member")
    seen.add(path_name)
    _check_tar_metadata(member, declared)
    _ensure_parents(staging, path_name, expected)
    destination = staging / path_name
    kind = declared["type"]

    if kind == "file":
        if member.type not in (tarfile.REGTYPE, tarfile.AREGTYPE) or member.sparse is not None:
            if member.type == tarfile.LNKTYPE:
                raise _error("hardlink_rejected", "archive hard links are not accepted")
            raise _error("special_member", "archive member is not a regular file")
        if member.size != declared["size_bytes"]:
            raise _error("member_size_mismatch", "archive file size differs from manifest")
        if member.linkname:
            raise _error("member_metadata_mismatch", "regular file has link metadata")
        fileobj = archive.extractfile(member)
        if fileobj is None:
            raise _error("member_payload_missing", "archive file payload is unavailable")
        _extract_file(fileobj, destination, declared)
        return

    if kind == "directory":
        if member.type != tarfile.DIRTYPE or member.size != 0:
            if member.type == tarfile.LNKTYPE:
                raise _error("hardlink_rejected", "archive hard links are not accepted")
            raise _error("special_member", "archive member is not a directory")
        if member.linkname:
            raise _error("member_metadata_mismatch", "directory has link metadata")
        try:
            metadata = destination.lstat()
        except FileNotFoundError:
            try:
                destination.mkdir(mode=declared["mode"])
                os.chmod(destination, declared["mode"], follow_symlinks=False)
            except OSError as error:
                raise _error("staging_io") from error
        except OSError as error:
            raise _error("staging_io") from error
        else:
            if not stat.S_ISDIR(metadata.st_mode):
                raise _error("tree_conflict", "directory member collides with another type")
            try:
                os.chmod(destination, declared["mode"], follow_symlinks=False)
            except OSError as error:
                raise _error("staging_io") from error
        return

    if kind == "symlink":
        if member.type != tarfile.SYMTYPE or member.size != 0:
            if member.type == tarfile.LNKTYPE:
                raise _error("hardlink_rejected", "archive hard links are not accepted")
            raise _error("special_member", "archive member is not a symbolic link")
        target = _safe_link_target(path_name, member.linkname)
        if target != declared["link_target"]:
            raise _error("member_link_mismatch", "archive symlink target differs from manifest")
        if _lexists(destination):
            raise _error("tree_conflict", "symlink member collides with another type")
        try:
            os.symlink(target, destination)
        except OSError as error:
            raise _error("staging_io") from error
        return

    raise _error("manifest_invalid", "unsupported runtime entry type")


def _stream_extract(
    archive_path: Path,
    staging: Path,
    expected: Mapping[str, Mapping[str, Any]],
) -> None:
    zstd = _require_zstd()
    stderr_file = tempfile.TemporaryFile(mode="w+b")
    try:
        process = subprocess.Popen(
            [zstd, "-q", "-d", "-c", "--", str(archive_path)],
            stdout=subprocess.PIPE,
            stderr=stderr_file,
            close_fds=True,
        )
    except OSError as error:
        stderr_file.close()
        raise _error("archive_decompression_failed") from error

    def stop_process() -> None:
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait()
        except OSError:
            pass

    if process.stdout is None:
        stop_process()
        stderr_file.close()
        raise _error("archive_stream_invalid")
    stdout = process.stdout
    seen: set[str] = set()
    try:
        with tarfile.open(fileobj=stdout, mode="r|") as tar:
            for member in tar:
                _extract_member(member, tar, staging, expected, seen)

        # tarfile stops at the first end-of-archive block.  Drain the zstd
        # pipe so a concatenated/trailing non-zero tar payload cannot be
        # silently ignored and so the child cannot block on a full pipe.
        trailing_nonzero = False
        while True:
            chunk = stdout.read(CHUNK_BYTES)
            if not chunk:
                break
            if any(chunk):
                trailing_nonzero = True
        return_code = process.wait()
        if return_code != 0:
            raise _error("archive_decompression_failed")
        if trailing_nonzero:
            raise _error("archive_trailing_data")
    except RuntimeMaterializerError:
        stop_process()
        raise
    except (OSError, EOFError, tarfile.TarError, ValueError) as error:
        stop_process()
        raise _error("archive_stream_invalid") from error
    finally:
        stdout.close()
        stderr_file.close()

    if seen != set(expected):
        raise _error("archive_entries_missing", "archive does not contain exactly the manifest entries")


def _walk_tree(root: Path) -> Iterator[tuple[str, Path, os.stat_result]]:
    stack: list[tuple[str, Path]] = [("", root)]
    while stack:
        relative, directory = stack.pop()
        try:
            with os.scandir(directory) as iterator:
                children = sorted(iterator, key=lambda item: item.name, reverse=True)
        except OSError as error:
            raise _error("verification_io") from error
        for child in children:
            child_relative = child.name if not relative else f"{relative}/{child.name}"
            if not is_safe_relative_path(child_relative):
                raise _error("verification_tree_unsafe")
            try:
                metadata = child.stat(follow_symlinks=False)
            except OSError as error:
                raise _error("verification_io") from error
            child_path = directory / child.name
            yield child_relative, child_path, metadata
            if stat.S_ISDIR(metadata.st_mode):
                stack.append((child_relative, child_path))


def _verify_symlink_inside(root: Path, path: Path, allowed: set[str]) -> None:
    try:
        resolved_root = root.resolve(strict=True)
        resolved = path.resolve(strict=True)
        resolved.relative_to(resolved_root)
        relative = resolved.relative_to(resolved_root).as_posix()
    except (OSError, RuntimeError, ValueError) as error:
        raise _error("verification_symlink", "runtime symlink is broken or escapes") from error
    if relative not in allowed:
        raise _error("verification_symlink", "runtime symlink target is not in the runtime tree")


def _verify_tree(root: Path, manifest: RuntimeManifest) -> None:
    """Complete filesystem verification, including unlisted extra entries."""

    if not _is_real_directory(root):
        raise _error("verification_root")
    expected = _expected_entries(manifest)
    implicit = _implicit_directories(expected)
    allowed = set(expected) | implicit
    actual: set[str] = set()
    for relative, path, metadata in _walk_tree(root):
        if relative in (MANIFEST_NAME, READY_NAME):
            continue
        actual.add(relative)
        if relative not in expected:
            if relative not in implicit or not stat.S_ISDIR(metadata.st_mode):
                raise _error("extra_materialized_entry", "materialized tree contains an unmanifested entry")

    if actual & {MANIFEST_NAME, READY_NAME}:
        raise _error("verification_metadata")
    missing = set(expected) - actual
    if missing:
        raise _error("missing_materialized_entry", "materialized tree is missing a manifest entry")

    for relative, entry in expected.items():
        path = root / relative
        try:
            metadata = path.lstat()
        except OSError as error:
            raise _error("missing_materialized_entry") from error
        if stat.S_IMODE(metadata.st_mode) != entry["mode"]:
            raise _error("materialized_mode_mismatch", "materialized mode differs from manifest")
        kind = entry["type"]
        if kind == "directory":
            if not stat.S_ISDIR(metadata.st_mode):
                raise _error("materialized_type_mismatch", "materialized type differs from manifest")
        elif kind == "file":
            if not stat.S_ISREG(metadata.st_mode):
                raise _error("materialized_type_mismatch", "materialized type differs from manifest")
            if metadata.st_size != entry["size_bytes"]:
                raise _error("materialized_size_mismatch", "materialized size differs from manifest")
            size, digest = _hash_regular_file(path)
            if size != entry["size_bytes"] or digest != entry["sha256"]:
                raise _error("materialized_digest_mismatch", "materialized digest differs from manifest")
        elif kind == "symlink":
            if not stat.S_ISLNK(metadata.st_mode):
                raise _error("materialized_type_mismatch", "materialized type differs from manifest")
            try:
                target = os.readlink(path)
            except OSError as error:
                raise _error("verification_symlink") from error
            if target != entry["link_target"]:
                raise _error("materialized_link_mismatch", "materialized symlink differs from manifest")
            _safe_link_target(relative, target)
            _verify_symlink_inside(root, path, set(expected))
        else:
            raise _error("manifest_invalid", "unsupported runtime entry type")


def _verify_metadata(root: Path, manifest_bytes: bytes, manifest: RuntimeManifest) -> None:
    manifest_path = root / MANIFEST_NAME
    ready_path = root / READY_NAME
    try:
        actual_manifest = manifest_path.lstat()
        actual_ready = ready_path.lstat()
    except OSError as error:
        raise _error("metadata_missing") from error
    if not stat.S_ISREG(actual_manifest.st_mode) or not stat.S_ISREG(actual_ready.st_mode):
        raise _error("metadata_invalid")
    try:
        if manifest_path.read_bytes() != manifest_bytes:
            raise _error("metadata_manifest_mismatch")
        ready_bytes = _read_regular_file(ready_path, max_bytes=MAX_READY_BYTES)
        ready_value = json.loads(ready_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise _error("metadata_invalid") from error
    try:
        expected_ready = build_ready_marker(manifest_bytes, manifest)
    except RuntimeReadyError as error:
        raise _error("metadata_invalid") from error
    if ready_value != expected_ready:
        raise _error("metadata_ready_mismatch")


def _verify_generation(root: Path, manifest_bytes: bytes, manifest: RuntimeManifest) -> None:
    _verify_metadata(root, manifest_bytes, manifest)
    _verify_tree(root, manifest)


def _atomic_update_current(runtime_root: Path, generation_name: str) -> bool:
    current = runtime_root / CURRENT_NAME
    if _lexists(current):
        try:
            metadata = current.lstat()
        except OSError as error:
            raise _error("current_update_failed") from error
        if not stat.S_ISLNK(metadata.st_mode):
            raise _error("current_not_symlink")
        try:
            if os.readlink(current) == generation_name:
                return False
        except OSError as error:
            raise _error("current_update_failed") from error

    temporary = runtime_root / f".{CURRENT_NAME}.{uuid.uuid4().hex}.partial"
    try:
        os.symlink(generation_name, temporary)
        os.replace(temporary, current)
        _fsync_directory(runtime_root)
    except RuntimeMaterializerError:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise _error("current_update_failed")
    except OSError as error:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise _error("current_update_failed") from error
    return True


def _publish_generation(runtime_root: Path, staging: Path, generation: Path) -> None:
    if _lexists(generation):
        raise _error("generation_conflict")
    try:
        os.rename(staging, generation)
        _fsync_directory(runtime_root)
    except OSError as error:
        raise _error("generation_publish_failed") from error


def _materialize_locked(
    archive_path: Path,
    manifest_bytes: bytes,
    manifest: RuntimeManifest,
    archive_size: int,
    archive_sha256: str,
    runtime_root: Path,
) -> dict[str, object]:
    runtime_hex = manifest["runtime_digest"].removeprefix("sha256:")
    generation_name = runtime_hex
    generation = runtime_root / generation_name
    expected = _expected_entries(manifest)

    if _lexists(generation):
        if not _is_real_directory(generation):
            raise _error("generation_conflict")
        _verify_generation(generation, manifest_bytes, manifest)
        current_updated = _atomic_update_current(runtime_root, generation_name)
        return {
            "status": "reused",
            "runtime_digest": manifest["runtime_digest"],
            "archive_size_bytes": archive_size,
            "archive_sha256": archive_sha256,
            "entry_count": len(expected),
            "current_updated": current_updated,
        }

    staging_root = runtime_root / STAGING_DIRECTORY
    _ensure_real_directory(staging_root, create=True)
    staging = staging_root / f"{runtime_hex}.{uuid.uuid4().hex}"
    try:
        staging.mkdir(mode=0o700)
    except OSError as error:
        raise _error("staging_io") from error
    published = False
    try:
        _stream_extract(archive_path, staging, expected)
        _verify_tree(staging, manifest)

        _atomic_write(staging / MANIFEST_NAME, manifest_bytes, mode=0o644)
        try:
            ready = build_ready_marker(manifest_bytes, manifest)
        except RuntimeReadyError as error:
            raise _error("metadata_invalid") from error
        _atomic_write(staging / READY_NAME, canonical_json(ready) + b"\n", mode=0o644)
        _verify_generation(staging, manifest_bytes, manifest)
        _fsync_tree_directories(staging)

        _publish_generation(runtime_root, staging, generation)
        published = True
        current_updated = _atomic_update_current(runtime_root, generation_name)
        return {
            "status": "materialized",
            "runtime_digest": manifest["runtime_digest"],
            "archive_size_bytes": archive_size,
            "archive_sha256": archive_sha256,
            "entry_count": len(expected),
            "current_updated": current_updated,
        }
    finally:
        if not published:
            _remove_tree(staging)


class RuntimeMaterializer:
    """Single-writer materializer for one local volume root."""

    def __init__(self, volume_root: Path, *, runtime_directory: str = RUNTIME_DIRECTORY) -> None:
        if (
            not runtime_directory
            or runtime_directory in (".", "..")
            or "/" in runtime_directory
            or "\\" in runtime_directory
            or any(ord(char) < 32 for char in runtime_directory)
        ):
            raise _error("runtime_root_invalid")
        self.volume_root = Path(volume_root)
        self.runtime_root = self.volume_root / runtime_directory

    def materialize(self, archive_path: Path, manifest_path: Path) -> dict[str, object]:
        manifest_bytes, manifest = _read_manifest(Path(manifest_path))
        archive_size, archive_sha256 = _validate_archive_input(Path(archive_path), manifest)

        _ensure_real_directory(self.volume_root, create=True)
        _ensure_real_directory(self.runtime_root, create=True)
        lock_path = self.runtime_root / LOCK_NAME
        with _writer_lock(lock_path):
            return _materialize_locked(
                Path(archive_path),
                manifest_bytes,
                manifest,
                archive_size,
                archive_sha256,
                self.runtime_root,
            )


def materialize_runtime(
    archive_path: Path,
    manifest_path: Path,
    volume_root: Path,
    *,
    runtime_directory: str = RUNTIME_DIRECTORY,
) -> dict[str, object]:
    """Materialize one archive and return bounded, path-free result metadata."""

    return RuntimeMaterializer(volume_root, runtime_directory=runtime_directory).materialize(
        archive_path,
        manifest_path,
    )


def materialize(
    archive_path: Path,
    manifest_path: Path,
    volume_root: Path,
    *,
    runtime_directory: str = RUNTIME_DIRECTORY,
) -> dict[str, object]:
    """Compatibility alias for callers using the shorter function name."""

    return materialize_runtime(
        archive_path,
        manifest_path,
        volume_root,
        runtime_directory=runtime_directory,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", "--archive-path", type=Path, required=True)
    parser.add_argument("--manifest", "--manifest-path", type=Path, required=True)
    parser.add_argument("--volume-root", "--volume", "--root", type=Path, required=True)
    parser.add_argument("--runtime-directory", default=RUNTIME_DIRECTORY)
    return parser


def _json_result(value: Mapping[str, object]) -> str:
    # All fields are scalar/bounded values assembled by this module.  Keep
    # separators compact so callers can safely treat one stdout line as one
    # result record.
    return json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = materialize_runtime(
            args.archive,
            args.manifest,
            args.volume_root,
            runtime_directory=args.runtime_directory,
        )
    except RuntimeMaterializerError as error:
        print(_json_result({"status": "error", "error": error.code}))
        return 2
    except (OSError, ValueError) as error:
        del error
        print(_json_result({"status": "error", "error": "materialization_failed"}))
        return 2
    print(_json_result(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
