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
import ctypes
from dataclasses import dataclass
import errno
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
    volume_mode_matches,
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
# The downloader runs this tiny capability probe after fetching the manifest
# and before fetching the (potentially multi-gigabyte) archive.  Keep the
# probe's footprint bounded even if a malformed-but-schema-valid manifest
# contains many distinct mode values.
MODE_PROBE_PREFIX = ".runtime-materializer-mode-probe-"
MAX_MODE_PROBE_VARIANTS = 64
MODE_PROBE_PRIVATE_MODE = 0o700
SYMLINK_MODE = 0o777
# The materializer's control directories are not part of the exported image
# tree.  They therefore have a small, provider-neutral publication contract:
#
# * ``.staging`` stays private while an archive is being expanded;
# * a published generation and any implicit parent directories are traversable
#   by an arbitrary runtime UID; a provider may normalize these directories to
#   the policy-observed ``0777`` mode;
# * generated metadata is readable by arbitrary runtime UIDs, but is not
#   writable by them.
#
# Regular-file modes from the runtime manifest are applied and verified through
# the capability policy below.  A provider may normalize non-executable files
# to 0666 and executable files to 0777; special bits remain rejected. Symlink
# modes stay strict at 0777.
PRIVATE_DIRECTORY_MODE = 0o700
RUNTIME_ROOT_MODE = 0o755
PUBLISHED_DIRECTORY_MODE = 0o755
PUBLISHED_METADATA_MODE = 0o644
STORAGE_EXHAUSTION_ERRNOS = frozenset(
    value
    for value in (errno.ENOSPC, getattr(errno, "EDQUOT", None), getattr(errno, "EFBIG", None))
    if value is not None
)
UNSUPPORTED_SYNC_ERRNOS = frozenset(
    value
    for value in (
        errno.EINVAL,
        getattr(errno, "ENOTSUP", None),
        getattr(errno, "EOPNOTSUPP", None),
    )
    if value is not None
)


class RuntimeMaterializerError(RuntimeError):
    """A fail-closed local materialization error.

    ``detail`` is useful to unit tests and callers using the Python API.  The
    command-line entrypoint deliberately emits only ``code`` so a local
    filesystem path can never appear in its bounded JSON output.
    """

    def __init__(
        self,
        code: str,
        detail: str | None = None,
        *,
        diagnostics: Mapping[str, int] | None = None,
    ) -> None:
        self.code = code
        self.reason = code
        self.detail = detail or code
        # Diagnostics are deliberately limited to non-negative integer
        # counters.  They are safe to forward through the short-lived result
        # callback and cannot contain paths, URLs, or filesystem error text.
        self.diagnostics = {
            key: value
            for key, value in (diagnostics or {}).items()
            if isinstance(key, str)
            and isinstance(value, int)
            and not isinstance(value, bool)
            and value >= 0
        }
        super().__init__(f"{code}: {self.detail}")


@dataclass(frozen=True)
class VolumeModePolicy:
    """Immutable mode behavior observed for one mounted volume.

    Directory modes may be preserved exactly or normalized by the provider to
    ``0777``. Ordinary files may be preserved exactly or normalized by
    execution class: non-executable files become ``0666`` and files with any
    execute bit become ``0777``. Files with special bits and symlinks remain
    strict. The policy is created by the bounded pre-download probe and passed
    through the complete materialization call so the final verifier cannot
    silently choose a different interpretation.
    """

    directory_modes: tuple[tuple[int, int], ...]
    file_modes: tuple[tuple[int, int], ...]
    symlink_mode: int = SYMLINK_MODE

    @staticmethod
    def _lookup(
        values: tuple[tuple[int, int], ...],
        expected_mode: int,
        *,
        entry_kind: int,
    ) -> int:
        for expected, actual in values:
            if expected == expected_mode:
                return actual
        raise _mode_probe_error(
            "mode_policy_incomplete",
            entry_kind=entry_kind,
            expected_mode=expected_mode,
        )

    def directory_actual_mode(self, expected_mode: int) -> int:
        return self._lookup(self.directory_modes, expected_mode, entry_kind=1)

    def file_actual_mode(self, expected_mode: int) -> int:
        return self._lookup(self.file_modes, expected_mode, entry_kind=2)

    @staticmethod
    def _validate_mapping(
        values: object,
        *,
        entry_kind: int,
        allow_directory_normalization: bool,
    ) -> None:
        """Reject malformed or ambiguous policy mappings before lookup."""

        if not isinstance(values, tuple):
            raise _error("mode_policy_invalid")
        seen: set[int] = set()
        for pair in values:
            if (
                not isinstance(pair, tuple)
                or len(pair) != 2
                or any(not isinstance(value, int) or isinstance(value, bool) for value in pair)
            ):
                raise _error("mode_policy_invalid")
            expected_mode, actual_mode = pair
            if (
                expected_mode < 0
                or expected_mode > 0o7777
                or actual_mode < 0
                or actual_mode > 0o7777
                or expected_mode in seen
            ):
                raise _error("mode_policy_invalid")
            seen.add(expected_mode)
            valid = volume_mode_matches(
                "directory" if entry_kind == 1 else "file",
                expected_mode,
                actual_mode,
            )
            if entry_kind == 1 and not allow_directory_normalization and actual_mode != expected_mode:
                valid = False
            if not valid:
                raise _mode_probe_error(
                    "mode_policy_invalid",
                    entry_kind=entry_kind,
                    expected_mode=expected_mode,
                    actual_mode=actual_mode,
                )

    def validate_manifest(self, manifest: RuntimeManifest) -> None:
        """Ensure this policy covers all manifest and materializer modes."""

        self._validate_mapping(
            self.directory_modes,
            entry_kind=1,
            allow_directory_normalization=True,
        )
        self._validate_mapping(
            self.file_modes,
            entry_kind=2,
            allow_directory_normalization=False,
        )
        if (
            not isinstance(self.symlink_mode, int)
            or isinstance(self.symlink_mode, bool)
            or self.symlink_mode != SYMLINK_MODE
        ):
            raise _mode_probe_error(
                "mode_policy_invalid",
                entry_kind=3,
                expected_mode=SYMLINK_MODE,
                actual_mode=self.symlink_mode,
            )
        required_directories = {0o700, 0o755}
        required_files = {0o600, 0o644, 0o755}
        for entry in manifest["file_tree"]["entries"]:
            if entry["type"] == "directory":
                required_directories.add(entry["mode"])
            elif entry["type"] == "file":
                required_files.add(entry["mode"])
            elif entry["type"] == "symlink" and entry["mode"] != self.symlink_mode:
                raise _mode_probe_error(
                    "materialized_mode_mismatch",
                    entry_kind=3,
                    expected_mode=entry["mode"],
                    actual_mode=self.symlink_mode,
                )
        for mode in required_directories:
            self.directory_actual_mode(mode)
        for mode in required_files:
            actual = self.file_actual_mode(mode)
            if not volume_mode_matches("file", mode, actual):
                raise _mode_probe_error(
                    "materialized_mode_mismatch",
                    entry_kind=2,
                    expected_mode=mode,
                    actual_mode=actual,
                )


def _error(
    code: str,
    detail: str | None = None,
    *,
    diagnostics: Mapping[str, int] | None = None,
) -> RuntimeMaterializerError:
    return RuntimeMaterializerError(code, detail, diagnostics=diagnostics)


def _is_storage_exhaustion(error: OSError) -> bool:
    """Return whether an OS error means the destination has no capacity."""

    return error.errno in STORAGE_EXHAUSTION_ERRNOS


def _volume_error(error: OSError, fallback: str) -> RuntimeMaterializerError:
    """Map provider-safe storage failures without exposing paths or errno text."""

    return _error("volume_capacity_exhausted" if _is_storage_exhaustion(error) else fallback)


def _volume_capacity_metrics(path: Path) -> dict[str, int]:
    """Return provider-safe block and inode counters for *path*.

    ``statvfs.f_bavail``/``f_favail`` describe capacity available to the
    materializer's unprivileged process.  That distinction matters on a
    mounted volume with reserved blocks: ``f_bfree`` can still be non-zero
    while a normal container process receives ``ENOSPC``.  The metrics are
    intentionally snapshots rather than promises; network filesystems can
    update them asynchronously.
    """

    stats = os.statvfs(path)
    fragment = stats.f_frsize or stats.f_bsize
    if fragment <= 0:
        raise OSError(errno.EIO, "invalid filesystem block size")
    free_inodes = getattr(stats, "f_favail", stats.f_ffree)
    return {
        "volume_total_bytes": max(0, int(fragment * stats.f_blocks)),
        "volume_free_bytes": max(0, int(fragment * stats.f_bavail)),
        "volume_total_inodes": max(0, int(stats.f_files)),
        "volume_free_inodes": max(0, int(free_inodes)),
    }


def _try_volume_capacity_metrics(path: Path) -> dict[str, int]:
    """Best-effort metrics for result diagnostics; never mask the real error."""

    try:
        return _volume_capacity_metrics(path)
    except (OSError, AttributeError, TypeError, ValueError):
        return {}


def _success_capacity_metrics(
    before: Mapping[str, int] | None,
    after: Mapping[str, int] | None,
) -> dict[str, int]:
    """Rename snapshot fields into stable success-result names."""

    fields: dict[str, int] = {}
    for snapshot_name, suffix in ((before, "before"), (after, "after")):
        if not snapshot_name:
            continue
        for key in ("volume_total_bytes", "volume_total_inodes"):
            value = snapshot_name.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                fields[key] = value
        for key in ("volume_free_bytes", "volume_free_inodes"):
            value = snapshot_name.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                fields[f"{key}_{suffix}"] = value
    return fields


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
        raise _volume_error(error, "volume_write_failed") from error
    if not _is_real_directory(path):
        raise _error("unsafe_volume_root")


def _chmod_directory(path: Path, mode: int, *, error_code: str) -> None:
    """Request a mode on a directory without following a symlink."""

    try:
        os.chmod(path, mode, follow_symlinks=False)
    except OSError as error:
        raise _volume_error(error, error_code) from error


def _apply_directory_mode(
    path: Path,
    expected_mode: int,
    mode_policy: VolumeModePolicy,
    *,
    error_code: str,
) -> None:
    """Apply one policy-bound directory mode and verify the stored result."""

    _chmod_directory(path, expected_mode, error_code=error_code)
    try:
        metadata = path.lstat()
    except OSError as error:
        raise _error(error_code) from error
    if not stat.S_ISDIR(metadata.st_mode):
        raise _error(error_code)
    actual_mode = stat.S_IMODE(metadata.st_mode)
    policy_mode = mode_policy.directory_actual_mode(expected_mode)
    if actual_mode != policy_mode:
        raise _mode_probe_error(
            "materialized_mode_mismatch",
            entry_kind=1,
            expected_mode=expected_mode,
            actual_mode=actual_mode,
        )


def _require_volume_root_traversable(path: Path) -> None:
    """Require the caller-supplied mount root to admit arbitrary readers.

    We deliberately do not chmod the volume root: it may contain unrelated
    provider-managed data and RunPod may mount it with permissive directory
    bits (for example ``0777``).  A runtime UID must nevertheless be able to
    read and traverse it to reach ``runtimes/current``.  The published
    ``runtimes`` directory and generations carry the actual no-write-by-other
    contract below; the provider's mount root is outside that contract.
    """

    try:
        metadata = path.lstat()
    except OSError as error:
        raise _error("volume_root_permissions") from error
    if not stat.S_ISDIR(metadata.st_mode):
        raise _error("unsafe_volume_root")
    mode = stat.S_IMODE(metadata.st_mode)
    if (mode & 0o005) != 0o005:
        raise _error("volume_root_permissions")


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
def _writer_lock(
    path: Path,
    mode_policy: VolumeModePolicy | None = None,
) -> Iterator[None]:
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
            if mode_policy is not None:
                metadata = os.fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode):
                    raise _error("lock_unavailable")
                actual_mode = stat.S_IMODE(metadata.st_mode)
                expected_mode = mode_policy.file_actual_mode(0o600)
                if actual_mode != expected_mode:
                    raise _mode_probe_error(
                        "materialized_mode_mismatch",
                        entry_kind=2,
                        expected_mode=0o600,
                        actual_mode=actual_mode,
                    )
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
        raise _volume_error(error, "volume_directory_sync_failed") from error
    try:
        os.fsync(descriptor)
    except OSError as error:
        # POSIX permits directory fsync to fail with EINVAL when the mounted
        # filesystem does not implement it.  RunPod Network Volumes are a
        # network filesystem, so retain fail-closed handling for real I/O
        # errors while relying on the filesystem-scoped barriers around
        # publication when directory fsync is explicitly unsupported.
        if error.errno in UNSUPPORTED_SYNC_ERRNOS:
            return
        raise _volume_error(error, "volume_directory_sync_failed") from error
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
            raise _error("volume_write_failed") from error
        for child in children:
            try:
                metadata = child.stat(follow_symlinks=False)
            except OSError as error:
                raise _volume_error(error, "volume_write_failed") from error
            if stat.S_ISDIR(metadata.st_mode):
                stack.append(directory / child.name)
    for directory in reversed(directories):
        _fsync_directory(directory)


def _sync_volume_filesystem(path: Path) -> None:
    """Flush file data for the filesystem containing *path* in one call.

    The runtime archive can contain hundreds of thousands of regular files.
    Calling ``fsync`` once per file makes materialization dominated by syscall
    latency on a Network Volume.  Linux ``syncfs`` provides the same durability
    boundary scoped to the mounted filesystem, so the complete staged tree is
    flushed once after it has passed content verification and before the
    generation is renamed into the published namespace.  A non-Linux test
    environment falls back to Python's system-wide ``sync`` because the
    materializer already requires a POSIX directory filesystem.
    """

    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError as error:
        raise _volume_error(error, "volume_sync_failed") from error
    try:
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            syncfs = libc.syncfs
        except (AttributeError, OSError):
            sync = getattr(os, "sync", None)
            if sync is None:
                raise _error("volume_sync_failed")
            try:
                sync()
            except OSError as error:
                raise _volume_error(error, "volume_sync_failed") from error
            return

        syncfs.argtypes = [ctypes.c_int]
        syncfs.restype = ctypes.c_int
        if syncfs(descriptor) != 0:
            error_number = ctypes.get_errno()
            if error_number not in UNSUPPORTED_SYNC_ERRNOS:
                raise OSError(error_number, os.strerror(error_number))
            # Some network filesystems reject syncfs even though Linux still
            # provides the system-wide sync barrier.  Falling back only for
            # the documented unsupported-operation errnos preserves genuine
            # I/O failures as terminal errors.
            sync = getattr(os, "sync", None)
            if sync is None:
                raise OSError(error_number, os.strerror(error_number))
            sync()
    except RuntimeMaterializerError:
        raise
    except OSError as error:
        raise _volume_error(error, "volume_sync_failed") from error
    finally:
        os.close(descriptor)


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
        raise _volume_error(error, "volume_write_failed") from error
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


def _mode_probe_error(
    code: str,
    *,
    entry_kind: int | None = None,
    expected_mode: int | None = None,
    actual_mode: int | None = None,
) -> RuntimeMaterializerError:
    """Build a bounded error for the pre-download filesystem capability probe."""

    diagnostics: dict[str, int] = {}
    for key, value in (
        ("entry_kind", entry_kind),
        ("expected_mode", expected_mode),
        ("actual_mode", actual_mode),
    ):
        if value is not None:
            diagnostics[key] = value
    return _error(code, diagnostics=diagnostics)


def _mode_probe_variants(manifest: RuntimeManifest) -> tuple[tuple[int, int], ...]:
    """Return the distinct regular-file and directory modes worth probing.

    The fixed modes map directly to materializer-owned paths: 0600 for locks
    and temporary files, 0644 for published metadata, 0755 for executable
    files, and 0700/0755 for private/published directories.  Manifest modes
    are included by kind so a provider that silently normalizes one mode class
    is caught before any archive bytes are downloaded.  Symlinks are probed
    separately because POSIX symlink mode bits are not a chmod round-trip
    surface.
    """

    modes: dict[int, set[int]] = {
        1: {0o700, 0o755},  # private/published directories
        2: {0o600, 0o644, 0o755},  # lock/metadata/executable files
    }
    for entry in manifest["file_tree"]["entries"]:
        kind = entry["type"]
        if kind == "directory":
            modes[1].add(entry["mode"])
        elif kind == "file":
            modes[2].add(entry["mode"])
    variants = tuple(
        (entry_kind, mode)
        for entry_kind in (1, 2)
        for mode in sorted(modes[entry_kind])
    )
    if len(variants) > MAX_MODE_PROBE_VARIANTS:
        raise _mode_probe_error("mode_probe_too_many_variants")
    return variants


def _probe_mode_entry(
    root: Path,
    entry_kind: int,
    expected_mode: int,
    index: int,
) -> tuple[int, int]:
    """Round-trip one mode and return ``(actual_kind, actual_mode)``."""

    label = "directory" if entry_kind == 1 else "file"
    path = root / f"{label}-{index}"
    try:
        if entry_kind == 2:
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(path, flags, MODE_PROBE_PRIVATE_MODE)
            try:
                os.fchmod(descriptor, expected_mode)
            finally:
                os.close(descriptor)
        else:
            path.mkdir(mode=MODE_PROBE_PRIVATE_MODE)
            os.chmod(path, expected_mode, follow_symlinks=False)
        metadata = path.lstat()
    except OSError as error:
        raise _volume_error(error, "mode_probe_failed") from error

    actual_kind = 1 if stat.S_ISDIR(metadata.st_mode) else 2 if stat.S_ISREG(metadata.st_mode) else 0
    actual_mode = stat.S_IMODE(metadata.st_mode)
    return actual_kind, actual_mode


def _probe_symlink_entry(root: Path, index: int) -> tuple[int, int]:
    """Create a symlink and return ``(actual_kind, actual_mode)``."""

    path = root / f"symlink-{index}"
    expected_mode = 0o777
    try:
        os.symlink("target", path)
        metadata = path.lstat()
    except OSError as error:
        raise _volume_error(error, "mode_probe_failed") from error

    actual_mode = stat.S_IMODE(metadata.st_mode)
    actual_kind = 3 if stat.S_ISLNK(metadata.st_mode) else 0
    return actual_kind, actual_mode


def _probe_observation_error(
    *,
    expected_kind: int,
    expected_mode: int,
    actual_kind: int,
    actual_mode: int,
) -> RuntimeMaterializerError | None:
    """Return a bounded error for one completed capability observation."""

    if actual_kind != expected_kind:
        return _mode_probe_error(
            "mode_probe_type_mismatch",
            entry_kind=expected_kind,
            expected_mode=expected_mode,
            actual_mode=actual_mode,
        )
    entry_type = {1: "directory", 2: "file", 3: "symlink"}.get(expected_kind)
    allowed = entry_type is not None and volume_mode_matches(
        entry_type,
        expected_mode,
        actual_mode,
    )
    if allowed:
        return None
    return _mode_probe_error(
        "materialized_mode_mismatch",
        entry_kind=expected_kind,
        expected_mode=expected_mode,
        actual_mode=actual_mode,
    )


def probe_volume_mode_capability(volume_root: Path, manifest: RuntimeManifest) -> VolumeModePolicy:
    """Verify the mounted volume preserves runtime POSIX modes before download.

    The probe is intentionally independent from ``runtimes/current`` and any
    existing generation. It creates one private, random directory directly
    below the caller-supplied mount, completes the entire bounded matrix, and
    removes the directory in all outcomes. A provider that reports chmod
    success but stores unsupported mode bits therefore fails closed before the
    expensive archive transfer and expansion begins.
    """

    root = Path(volume_root)
    _require_volume_root_traversable(root)
    variants = _mode_probe_variants(manifest)
    probe: Path | None = None
    primary_error: RuntimeMaterializerError | None = None
    directory_modes: dict[int, int] = {}
    file_modes: dict[int, int] = {}
    try:
        try:
            probe = Path(tempfile.mkdtemp(prefix=MODE_PROBE_PREFIX, dir=root))
            os.chmod(probe, MODE_PROBE_PRIVATE_MODE, follow_symlinks=False)
        except OSError as error:
            raise _volume_error(error, "mode_probe_failed") from error
        for index, (entry_kind, expected_mode) in enumerate(variants):
            try:
                actual_kind, actual_mode = _probe_mode_entry(
                    probe,
                    entry_kind,
                    expected_mode,
                    index,
                )
                observation_error = _probe_observation_error(
                    expected_kind=entry_kind,
                    expected_mode=expected_mode,
                    actual_kind=actual_kind,
                    actual_mode=actual_mode,
                )
                if observation_error is not None and primary_error is None:
                    primary_error = observation_error
                if entry_kind == 1:
                    directory_modes[expected_mode] = actual_mode
                else:
                    file_modes[expected_mode] = actual_mode
            except RuntimeMaterializerError as error:
                if primary_error is None:
                    primary_error = error
        try:
            actual_kind, actual_mode = _probe_symlink_entry(probe, len(variants))
            observation_error = _probe_observation_error(
                expected_kind=3,
                expected_mode=SYMLINK_MODE,
                actual_kind=actual_kind,
                actual_mode=actual_mode,
            )
            if observation_error is not None and primary_error is None:
                primary_error = observation_error
        except RuntimeMaterializerError as error:
            if primary_error is None:
                primary_error = error
    except RuntimeMaterializerError as error:
        # Cleanup is handled below so a cleanup failure can be reported as a
        # bounded diagnostic without replacing the primary capability result.
        if primary_error is None:
            primary_error = error
    finally:
        cleanup_failed = False
        if probe is not None:
            _remove_tree(probe)
            if _lexists(probe):
                cleanup_failed = True
        if cleanup_failed:
            if primary_error is not None:
                primary_error.diagnostics["mode_probe_cleanup_failed"] = 1
            else:
                primary_error = _error(
                    "mode_probe_cleanup_failed",
                    diagnostics={"mode_probe_cleanup_failed": 1},
                )
    if primary_error is not None:
        raise primary_error
    policy = VolumeModePolicy(
        directory_modes=tuple(sorted(directory_modes.items())),
        file_modes=tuple(sorted(file_modes.items())),
    )
    policy.validate_manifest(manifest)
    return policy


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
                raise _volume_error(error, "volume_write_failed") from error
            continue
        except OSError as error:
            raise _error("volume_write_failed") from error
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
        raise _volume_error(error, "volume_write_failed") from error


def _extract_file(fileobj: Any, path: Path, expected: Mapping[str, Any]) -> None:
    descriptor = _open_new_file(path, expected["mode"])
    digest = hashlib.sha256()
    size = 0
    try:
        while True:
            try:
                chunk = fileobj.read(CHUNK_BYTES)
            except OSError as error:
                # This read is from the decompressor pipe, not the Network
                # Volume.  Keep source-stream failures distinct from the
                # destination write failures below.
                raise _error("archive_stream_invalid") from error
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
                raise _volume_error(error, "volume_write_failed") from error
        if size != expected["size_bytes"] or digest.hexdigest() != expected["sha256"]:
            raise _error("member_digest_mismatch", "archive file content differs from manifest")
    finally:
        os.close(descriptor)


def _consume_file(
    fileobj: Any,
    expected: Mapping[str, Any],
) -> tuple[int, str]:
    """Consume and hash a tar payload without publishing its bytes."""

    digest = hashlib.sha256()
    size = 0
    while True:
        try:
            chunk = fileobj.read(CHUNK_BYTES)
        except OSError as error:
            raise _error("archive_stream_invalid") from error
        if not chunk:
            break
        size += len(chunk)
        digest.update(chunk)
    actual = digest.hexdigest()
    if size != expected["size_bytes"] or actual != expected["sha256"]:
        raise _error("member_digest_mismatch", "archive file content differs from manifest")
    return size, actual


def _extract_or_link_file(
    fileobj: Any,
    path: Path,
    expected: Mapping[str, Any],
    canonical_path: Path | None,
) -> bool:
    """Verify one payload and optionally publish it as a hardlink.

    The manifest is sorted by path, so a dedupe target selected from the
    first occurrence of each `(sha256, size, mode)` key is encountered before
    its aliases in archives produced by the exporter.  We still fall back to
    a normal write if a non-conforming archive presents an alias first; this
    preserves correctness without trusting archive ordering.
    """

    if canonical_path is None or not canonical_path.is_file():
        _extract_file(fileobj, path, expected)
        return False

    _consume_file(fileobj, expected)
    try:
        os.link(canonical_path, path)
    except OSError as error:
        # The payload has already been consumed and cannot be replayed from a
        # streaming tar.  A filesystem that rejects hardlinks is therefore a
        # terminal publication failure rather than a silent duplicate write.
        raise _volume_error(error, "volume_write_failed") from error
    return True


def _dedupe_targets(expected: Mapping[str, Mapping[str, Any]]) -> dict[str, str]:
    """Map duplicate regular files to the first path with identical metadata."""

    canonical: dict[tuple[str, int, int], str] = {}
    aliases: dict[str, str] = {}
    for path, entry in expected.items():
        if entry["type"] != "file":
            continue
        key = (entry["sha256"], entry["size_bytes"], entry["mode"])
        first = canonical.setdefault(key, path)
        if first != path:
            aliases[path] = first
    return aliases


def _extract_member(
    member: tarfile.TarInfo,
    archive: tarfile.TarFile,
    staging: Path,
    expected: Mapping[str, Mapping[str, Any]],
    seen: set[str],
    dedupe_targets: Mapping[str, str],
) -> bool:
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
        return _extract_or_link_file(
            fileobj,
            destination,
            declared,
            staging / dedupe_targets[path_name] if path_name in dedupe_targets else None,
        )

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
                raise _volume_error(error, "volume_write_failed") from error
        except OSError as error:
            raise _error("volume_write_failed") from error
        else:
            if not stat.S_ISDIR(metadata.st_mode):
                raise _error("tree_conflict", "directory member collides with another type")
            try:
                os.chmod(destination, declared["mode"], follow_symlinks=False)
            except OSError as error:
                raise _volume_error(error, "volume_write_failed") from error
        return False

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
            raise _volume_error(error, "volume_write_failed") from error
        return False

    raise _error("manifest_invalid", "unsupported runtime entry type")


def _stream_extract(
    archive_path: Path,
    staging: Path,
    expected: Mapping[str, Mapping[str, Any]],
) -> dict[str, int]:
    zstd = _require_zstd()
    try:
        stderr_file = tempfile.TemporaryFile(mode="w+b")
    except OSError as error:
        if _is_storage_exhaustion(error):
            raise _error("archive_temporary_disk_exhausted") from error
        raise _error("archive_temporary_io") from error
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
    dedupe_targets = _dedupe_targets(expected)
    hardlink_count = 0
    hardlink_saved_bytes = 0
    try:
        with tarfile.open(fileobj=stdout, mode="r|") as tar:
            for member in tar:
                path_name = _safe_member_path(member.name)
                linked = _extract_member(member, tar, staging, expected, seen, dedupe_targets)
                if linked:
                    hardlink_count += 1
                    hardlink_saved_bytes += int(expected[path_name]["size_bytes"])

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
    except OSError as error:
        stop_process()
        if _is_storage_exhaustion(error):
            # A full destination can surface through the pipe after the
            # writer has failed.  Do not misreport that as corrupt archive
            # bytes; the bounded public code identifies volume capacity.
            raise _error("volume_capacity_exhausted") from error
        raise _error("archive_stream_invalid") from error
    except (EOFError, tarfile.TarError, ValueError) as error:
        stop_process()
        raise _error("archive_stream_invalid") from error
    finally:
        stdout.close()
        stderr_file.close()

    if seen != set(expected):
        raise _error("archive_entries_missing", "archive does not contain exactly the manifest entries")
    return {
        "hardlink_count": hardlink_count,
        "hardlink_saved_bytes": hardlink_saved_bytes,
    }


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


def _verify_tree(
    root: Path,
    manifest: RuntimeManifest,
    mode_policy: VolumeModePolicy,
    *,
    allow_unmanifested: bool = False,
) -> None:
    """Verify all published entries and optionally tolerate runtime-created files."""

    if not _is_real_directory(root):
        raise _error("verification_root")
    expected = _expected_entries(manifest)
    implicit = _implicit_directories(expected)
    actual: set[str] = set()
    for relative, path, metadata in _walk_tree(root):
        if relative in (MANIFEST_NAME, READY_NAME):
            continue
        actual.add(relative)
        if relative not in expected:
            if not allow_unmanifested and (
                relative not in implicit or not stat.S_ISDIR(metadata.st_mode)
            ):
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
        kind = entry["type"]
        actual_mode = stat.S_IMODE(metadata.st_mode)
        if kind == "directory":
            if not stat.S_ISDIR(metadata.st_mode):
                raise _error("materialized_type_mismatch", "materialized type differs from manifest")
            expected_mode = mode_policy.directory_actual_mode(entry["mode"])
            if actual_mode != expected_mode:
                raise _mode_probe_error(
                    "materialized_mode_mismatch",
                    entry_kind=1,
                    expected_mode=entry["mode"],
                    actual_mode=actual_mode,
                )
        elif kind == "file":
            if not stat.S_ISREG(metadata.st_mode):
                raise _error("materialized_type_mismatch", "materialized type differs from manifest")
            expected_mode = mode_policy.file_actual_mode(entry["mode"])
            if actual_mode != expected_mode:
                raise _mode_probe_error(
                    "materialized_mode_mismatch",
                    entry_kind=2,
                    expected_mode=entry["mode"],
                    actual_mode=actual_mode,
                )
            if metadata.st_size != entry["size_bytes"]:
                raise _error("materialized_size_mismatch", "materialized size differs from manifest")
            size, digest = _hash_regular_file(path)
            if size != entry["size_bytes"] or digest != entry["sha256"]:
                raise _error("materialized_digest_mismatch", "materialized digest differs from manifest")
        elif kind == "symlink":
            if not stat.S_ISLNK(metadata.st_mode):
                raise _error("materialized_type_mismatch", "materialized type differs from manifest")
            if entry["mode"] != mode_policy.symlink_mode or actual_mode != mode_policy.symlink_mode:
                raise _mode_probe_error(
                    "materialized_mode_mismatch",
                    entry_kind=3,
                    expected_mode=entry["mode"],
                    actual_mode=actual_mode,
                )
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


def _verify_metadata(
    root: Path,
    manifest_bytes: bytes,
    manifest: RuntimeManifest,
    mode_policy: VolumeModePolicy,
) -> None:
    manifest_path = root / MANIFEST_NAME
    ready_path = root / READY_NAME
    try:
        actual_manifest = manifest_path.lstat()
        actual_ready = ready_path.lstat()
    except OSError as error:
        raise _error("metadata_missing") from error
    if not stat.S_ISREG(actual_manifest.st_mode) or not stat.S_ISREG(actual_ready.st_mode):
        raise _error("metadata_invalid")
    expected_metadata_mode = mode_policy.file_actual_mode(PUBLISHED_METADATA_MODE)
    if (
        stat.S_IMODE(actual_manifest.st_mode) != expected_metadata_mode
        or stat.S_IMODE(actual_ready.st_mode) != expected_metadata_mode
    ):
        raise _error("metadata_mode_mismatch")
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


def _verify_generation(
    root: Path,
    manifest_bytes: bytes,
    manifest: RuntimeManifest,
    mode_policy: VolumeModePolicy,
    *,
    allow_unmanifested: bool = False,
) -> None:
    _verify_metadata(root, manifest_bytes, manifest, mode_policy)
    _verify_tree(root, manifest, mode_policy, allow_unmanifested=allow_unmanifested)


def _seal_published_generation(
    root: Path,
    manifest: RuntimeManifest,
    mode_policy: VolumeModePolicy,
) -> None:
    """Seal only materializer-owned directories after full verification.

    The exported runtime files and symlinks retain the exact modes recorded in
    the manifest. Only the generation root and parent directories that were
    synthesized because they were absent from that manifest are materializer
    metadata; requesting ``0755`` lets a Pod running as any UID reach the
    verified runtime, subject to the provider's pre-probed directory policy.
    The operation is intentionally idempotent so a crash between generation
    rename and ``current`` publication can be repaired on retry.
    """

    expected = _expected_entries(manifest)
    directories = [root, *(root / path for path in sorted(_implicit_directories(expected)))]
    for directory in directories:
        try:
            metadata = directory.lstat()
        except OSError as error:
            raise _error("publication_permissions") from error
        if not stat.S_ISDIR(metadata.st_mode):
            raise _error("publication_permissions")
        _apply_directory_mode(
            directory,
            PUBLISHED_DIRECTORY_MODE,
            mode_policy,
            error_code="publication_permissions",
        )
        _fsync_directory(directory)


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
        raise
    except OSError as error:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise _volume_error(error, "current_update_failed") from error
    return True


def _publish_generation(runtime_root: Path, staging: Path, generation: Path) -> None:
    if _lexists(generation):
        raise _error("generation_conflict")
    try:
        os.rename(staging, generation)
        _fsync_directory(runtime_root)
    except OSError as error:
        raise _volume_error(error, "generation_publish_failed") from error


def _materialize_locked(
    archive_path: Path,
    manifest_bytes: bytes,
    manifest: RuntimeManifest,
    archive_size: int,
    archive_sha256: str,
    runtime_root: Path,
    mode_policy: VolumeModePolicy,
    capacity_before: Mapping[str, int] | None = None,
) -> dict[str, object]:
    runtime_hex = manifest["runtime_digest"].removeprefix("sha256:")
    generation_name = runtime_hex
    generation = runtime_root / generation_name
    expected = _expected_entries(manifest)

    if _lexists(generation):
        if not _is_real_directory(generation):
            raise _error("generation_conflict")
        # Product Pods may add trusted runtime-local caches such as
        # ``__pycache__`` after initial publication. Reuse still verifies every
        # manifest-owned entry and the exact manifest/READY metadata, but does
        # not reject additional files created by the curated runtime.
        _verify_generation(
            generation,
            manifest_bytes,
            manifest,
            mode_policy,
            allow_unmanifested=True,
        )
        # A previous process may have been interrupted after the generation
        # rename but before sealing its materializer-owned parent directories.
        # Repair that state before exposing it through current.
        _seal_published_generation(generation, manifest, mode_policy)
        _sync_volume_filesystem(runtime_root)
        current_updated = _atomic_update_current(runtime_root, generation_name)
        # Directory fsync is optional on some network filesystems.  Finish
        # with a filesystem-scoped barrier after every metadata rename so a
        # successful result never outruns publication durability.
        _sync_volume_filesystem(runtime_root)
        result: dict[str, object] = {
            "status": "reused",
            "runtime_digest": manifest["runtime_digest"],
            "archive_size_bytes": archive_size,
            "archive_sha256": archive_sha256,
            "entry_count": len(expected),
            "materialized_bytes": manifest["file_tree"]["total_bytes"],
            "current_updated": current_updated,
            "hardlink_count": 0,
            "hardlink_saved_bytes": 0,
        }
        result.update(_success_capacity_metrics(capacity_before, _try_volume_capacity_metrics(runtime_root)))
        return result

    staging_root = runtime_root / STAGING_DIRECTORY
    _ensure_real_directory(staging_root, create=True)
    _apply_directory_mode(
        staging_root,
        PRIVATE_DIRECTORY_MODE,
        mode_policy,
        error_code="volume_write_failed",
    )
    staging = staging_root / f"{runtime_hex}.{uuid.uuid4().hex}"
    published = False
    try:
        try:
            staging.mkdir(mode=PRIVATE_DIRECTORY_MODE)
        except OSError as error:
            raise _volume_error(error, "volume_write_failed") from error
        _apply_directory_mode(
            staging,
            PRIVATE_DIRECTORY_MODE,
            mode_policy,
            error_code="volume_write_failed",
        )
        extraction_metrics = _stream_extract(archive_path, staging, expected)
        _verify_tree(staging, manifest, mode_policy)

        _atomic_write(staging / MANIFEST_NAME, manifest_bytes, mode=PUBLISHED_METADATA_MODE)
        try:
            ready = build_ready_marker(manifest_bytes, manifest)
        except RuntimeReadyError as error:
            raise _error("metadata_invalid") from error
        _atomic_write(
            staging / READY_NAME,
            canonical_json(ready) + b"\n",
            mode=PUBLISHED_METADATA_MODE,
        )
        _verify_generation(staging, manifest_bytes, manifest, mode_policy)
        # Every regular file has already passed a complete size and digest
        # verification. Flush the staged generation once at filesystem scope
        # instead of issuing one fsync syscall per archive member.
        _sync_volume_filesystem(runtime_root)

        _publish_generation(runtime_root, staging, generation)
        published = True
        # Keep the rename-before-seal order: staging remains private until it
        # is complete, while an interrupted seal is recoverable by the
        # existing-generation path above and never becomes current.
        _seal_published_generation(generation, manifest, mode_policy)
        _sync_volume_filesystem(runtime_root)
        current_updated = _atomic_update_current(runtime_root, generation_name)
        # Directory fsync is optional on some network filesystems.  Finish
        # with a filesystem-scoped barrier after every metadata rename so a
        # successful result never outruns publication durability.
        _sync_volume_filesystem(runtime_root)
        result = {
            "status": "materialized",
            "runtime_digest": manifest["runtime_digest"],
            "archive_size_bytes": archive_size,
            "archive_sha256": archive_sha256,
            "entry_count": len(expected),
            "materialized_bytes": manifest["file_tree"]["total_bytes"],
            "current_updated": current_updated,
            **extraction_metrics,
        }
        result.update(_success_capacity_metrics(capacity_before, _try_volume_capacity_metrics(runtime_root)))
        return result
    except RuntimeMaterializerError as error:
        # Capture the mounted filesystem while the failed staging generation
        # still exists.  The finally block below removes that private tree;
        # taking this snapshot in the outer caller would otherwise make a
        # capacity failure look as though it never consumed the blocks/inodes
        # that triggered it.
        error.diagnostics.update(_try_volume_capacity_metrics(runtime_root))
        raise
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

    def materialize(
        self,
        archive_path: Path,
        manifest_path: Path,
        *,
        mode_policy: VolumeModePolicy | None = None,
    ) -> dict[str, object]:
        manifest_bytes, manifest = _read_manifest(Path(manifest_path))
        archive_size, archive_sha256 = _validate_archive_input(Path(archive_path), manifest)

        _ensure_real_directory(self.volume_root, create=True)
        _require_volume_root_traversable(self.volume_root)
        if mode_policy is None:
            mode_policy = probe_volume_mode_capability(self.volume_root, manifest)
        mode_policy.validate_manifest(manifest)
        _ensure_real_directory(self.runtime_root, create=True)
        _apply_directory_mode(
            self.runtime_root,
            RUNTIME_ROOT_MODE,
            mode_policy,
            error_code="volume_write_failed",
        )
        lock_path = self.runtime_root / LOCK_NAME
        with _writer_lock(lock_path, mode_policy):
            capacity_before = _try_volume_capacity_metrics(self.volume_root)
            try:
                return _materialize_locked(
                    Path(archive_path),
                    manifest_bytes,
                    manifest,
                    archive_size,
                    archive_sha256,
                    self.runtime_root,
                    mode_policy,
                    capacity_before,
                )
            except RuntimeMaterializerError as error:
                # Preserve the bounded primary code while attaching the
                # immutable expected totals. The inner materializer captures
                # the live capacity snapshot before its staging cleanup; the
                # caller must not overwrite that failure-time evidence. A
                # best-effort post-cleanup snapshot is still useful for early
                # failures that occurred before the inner extraction try.
                for key, value in _try_volume_capacity_metrics(self.volume_root).items():
                    error.diagnostics.setdefault(key, value)
                error.diagnostics.update({
                    "expected_materialized_bytes": int(manifest["file_tree"]["total_bytes"]),
                    "expected_entry_count": len(manifest["file_tree"]["entries"]),
                    "archive_size_bytes": archive_size,
                })
                raise


def materialize_runtime(
    archive_path: Path,
    manifest_path: Path,
    volume_root: Path,
    *,
    runtime_directory: str = RUNTIME_DIRECTORY,
    mode_policy: VolumeModePolicy | None = None,
) -> dict[str, object]:
    """Materialize one archive and return bounded, path-free result metadata."""

    return RuntimeMaterializer(volume_root, runtime_directory=runtime_directory).materialize(
        archive_path,
        manifest_path,
        mode_policy=mode_policy,
    )


def materialize(
    archive_path: Path,
    manifest_path: Path,
    volume_root: Path,
    *,
    runtime_directory: str = RUNTIME_DIRECTORY,
    mode_policy: VolumeModePolicy | None = None,
) -> dict[str, object]:
    """Compatibility alias for callers using the shorter function name."""

    return materialize_runtime(
        archive_path,
        manifest_path,
        volume_root,
        runtime_directory=runtime_directory,
        mode_policy=mode_policy,
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
