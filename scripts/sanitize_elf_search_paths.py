#!/usr/bin/env python3
"""Remove empty ELF RPATH/RUNPATH components during image construction.

An empty component in ``DT_RPATH`` or ``DT_RUNPATH`` means the current working
directory to the ELF loader.  That is unsafe for the flattened ComfyComplete
runtime, so this build-time tool removes only those components.  It is
deliberately fail-closed: a missing tool, malformed ``readelf`` output, a
failed patch, or a post-patch mismatch aborts the build.

The production invocation scans exactly ``/opt/conda`` and ``/app/comfyui``.
``--root`` exists only to make the same bounded scan testable against a
temporary rootfs; it never changes the two selected paths.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


ELF_MAGIC = b"\x7fELF"
TARGETS = (Path("/opt/conda"), Path("/app/comfyui"))
TOOL_TIMEOUT_SECONDS = 30.0

_DYNAMIC_HEADER = re.compile(r"^\s*Dynamic section at offset .+ contains (\d+) entries:\s*$")
_NO_DYNAMIC_SECTION = "There is no dynamic section in this file."
_DYNAMIC_ENTRY = re.compile(r"^\s*(0x[0-9A-Fa-f]+)\s+\(([^)]+)\)\s+(.+?)\s*$")
_DYNAMIC_COLUMNS = re.compile(r"^\s*Tag\s+Type\s+Name/Value\s*$")
_SEARCH_DETAIL = re.compile(r"^Library (rpath|runpath): \[(.*)\]$")


class SanitizerError(RuntimeError):
    """Raised for any scan, tool, parsing, or verification failure."""


@dataclass(frozen=True)
class DynamicEntry:
    """One stable ``readelf -dW`` dynamic table entry."""

    tag: str
    detail: str


@dataclass(frozen=True)
class DynamicInfo:
    """Parsed dynamic table and its RPATH/RUNPATH values."""

    entries: tuple[DynamicEntry, ...]
    search_paths: tuple[tuple[str, str], ...]

    def entries_without_search_paths(self) -> tuple[DynamicEntry, ...]:
        return tuple(entry for entry in self.entries if entry.tag not in {"RPATH", "RUNPATH"})


@dataclass(frozen=True)
class SanitizationReport:
    """Stable result suitable for the build log and tests."""

    scanned_elf_files: int
    modified_files: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "modified_files": list(self.modified_files),
            "modified_elf_files": len(self.modified_files),
            "scanned_elf_files": self.scanned_elf_files,
        }


def remove_empty_components(value: str) -> str:
    """Drop empty colon-separated components, retaining every other component.

    In particular, non-empty duplicates are intentionally retained.  This
    operation changes only the unsafe empty components and preserves the
    loader's original order and duplicate-entry behavior.
    """

    components = value.split(":")
    return ":".join(component for component in components if component)


def has_empty_component(value: str) -> bool:
    """Return whether a search path contains a leading, trailing, or doubled colon."""

    return any(component == "" for component in value.split(":"))


def _tool_path(command: str | None, name: str) -> str:
    candidate = command or shutil.which(name)
    if not candidate:
        raise SanitizerError(f"required tool is unavailable: {name}")
    tool = Path(candidate)
    try:
        mode = os.stat(tool).st_mode
    except OSError as exc:
        raise SanitizerError(f"cannot inspect {name}: {candidate}: {exc}") from exc
    if not stat.S_ISREG(mode) or not os.access(tool, os.X_OK):
        raise SanitizerError(f"{name} is not an executable regular file: {candidate}")
    return candidate


def _run_tool(command: Sequence[str], *, tool_name: str) -> str:
    """Run a tool with a deterministic locale and reject all tool failures."""

    environment = os.environ.copy()
    environment["LC_ALL"] = "C"
    environment["LANG"] = "C"
    try:
        completed = subprocess.run(
            list(command),
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=TOOL_TIMEOUT_SECONDS,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SanitizerError(f"{tool_name} failed to run: {exc}") from exc
    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace").strip()
        raise SanitizerError(
            f"{tool_name} exited {completed.returncode}"
            + (f": {stderr}" if stderr else "")
        )
    if completed.stderr:
        stderr = completed.stderr.decode("utf-8", errors="replace").strip()
        raise SanitizerError(f"{tool_name} wrote to stderr: {stderr}")
    try:
        return completed.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SanitizerError(f"{tool_name} emitted non-UTF-8 output") from exc


def parse_dynamic(output: str, *, path: Path) -> DynamicInfo:
    """Parse the complete dynamic table from ``readelf -dW`` output.

    The parser accepts a static ELF (which has no dynamic section), but it
    rejects output that looks truncated or contains an unparseable dynamic
    entry.  This prevents a tool/version mismatch from silently skipping a
    dangerous search path.
    """

    lines = output.splitlines()
    header_index = next((index for index, line in enumerate(lines) if _DYNAMIC_HEADER.match(line)), None)
    if header_index is None:
        if any(_NO_DYNAMIC_SECTION in line for line in lines):
            return DynamicInfo(entries=(), search_paths=())
        raise SanitizerError(f"cannot locate readelf dynamic section for {path}")

    header_match = _DYNAMIC_HEADER.match(lines[header_index])
    if header_match is None:
        raise SanitizerError(f"cannot parse readelf dynamic header for {path}")
    expected_entries = int(header_match.group(1))
    entries: list[DynamicEntry] = []
    search_paths: list[tuple[str, str]] = []
    saw_entry = False
    for line in lines[header_index + 1 :]:
        if line.strip() == "":
            break
        if not saw_entry and _DYNAMIC_COLUMNS.match(line):
            continue
        match = _DYNAMIC_ENTRY.match(line)
        if match is None:
            # Dynamic output ends at a blank line, so a non-blank line after
            # the header is either an entry or malformed/truncated output.
            raise SanitizerError(f"cannot parse readelf dynamic entry for {path}: {line!r}")
        _tag_number, tag, detail = match.groups()
        saw_entry = True
        entry = DynamicEntry(tag=tag, detail=detail)
        entries.append(entry)
        if tag in {"RPATH", "RUNPATH"}:
            search_match = _SEARCH_DETAIL.fullmatch(detail)
            expected_label = "rpath" if tag == "RPATH" else "runpath"
            if search_match is None or search_match.group(1) != expected_label:
                raise SanitizerError(f"cannot parse {tag} value for {path}: {detail!r}")
            search_paths.append((tag, search_match.group(2)))

    if len(entries) != expected_entries:
        raise SanitizerError(
            f"readelf dynamic table is incomplete for {path}: "
            f"expected {expected_entries} entries, parsed {len(entries)}"
        )
    return DynamicInfo(entries=tuple(entries), search_paths=tuple(search_paths))


def read_dynamic(path: Path, *, readelf: str) -> DynamicInfo:
    """Read and parse one ELF's dynamic table."""

    if not _is_elf_regular(path):
        raise SanitizerError(f"path is not a real ELF regular file: {path}")
    output = _run_tool((readelf, "-dW", str(path)), tool_name="readelf")
    return parse_dynamic(output, path=path)


def _lstat_regular(path: Path) -> os.stat_result | None:
    try:
        result = os.lstat(path)
    except OSError as exc:
        raise SanitizerError(f"cannot inspect {path}: {exc}") from exc
    if not stat.S_ISREG(result.st_mode):
        return None
    return result


def _is_elf_regular(path: Path) -> bool:
    """Check a path without ever following a symlink."""

    result = _lstat_regular(path)
    if result is None:
        return False
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise SanitizerError(f"cannot read {path}: {exc}") from exc
    try:
        return os.read(descriptor, len(ELF_MAGIC)) == ELF_MAGIC
    except OSError as exc:
        raise SanitizerError(f"cannot read {path}: {exc}") from exc
    finally:
        os.close(descriptor)


def _target_path(root: Path, target: Path) -> Path:
    if root == Path("/"):
        return target
    return root / target.relative_to("/")


def iter_elf_files(root: Path = Path("/")) -> Iterable[Path]:
    """Yield sorted, real ELF regular files under the two fixed targets."""

    root = root.resolve()
    found: list[Path] = []
    for target in TARGETS:
        selected = _target_path(root, target)
        try:
            target_stat = os.lstat(selected)
        except OSError as exc:
            raise SanitizerError(f"cannot inspect scan target {selected}: {exc}") from exc
        if not stat.S_ISDIR(target_stat.st_mode):
            raise SanitizerError(f"scan target is not a real directory: {selected}")

        def onerror(error: OSError) -> None:
            raise SanitizerError(f"cannot scan {selected}: {error}") from error

        for directory, dirnames, filenames in os.walk(
            selected,
            topdown=True,
            followlinks=False,
            onerror=onerror,
        ):
            dirnames.sort()
            filenames.sort()
            for filename in filenames:
                candidate = Path(directory) / filename
                if _is_elf_regular(candidate):
                    found.append(candidate)
    yield from sorted(found)


def _backup_file(path: Path, metadata: os.stat_result) -> Path:
    """Create a same-directory metadata-preserving backup without loading it in memory."""

    descriptor, backup_name = tempfile.mkstemp(
        prefix=f".{path.name}.elf-sanitize-",
        dir=path.parent,
    )
    backup_path = Path(backup_name)
    try:
        source_flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            source_flags |= os.O_NOFOLLOW
        source_descriptor = os.open(path, source_flags)
        try:
            with os.fdopen(source_descriptor, "rb") as source, os.fdopen(descriptor, "wb") as backup:
                descriptor = -1
                shutil.copyfileobj(source, backup, length=1024 * 1024)
                os.fchown(backup.fileno(), metadata.st_uid, metadata.st_gid)
                os.fchmod(backup.fileno(), stat.S_IMODE(metadata.st_mode))
                backup.flush()
                os.fsync(backup.fileno())
        except BaseException:
            # The context managers close descriptors even when a copy fails.
            raise
    except BaseException as exc:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            backup_path.unlink()
        except OSError:
            pass
        if isinstance(exc, SanitizerError):
            raise
        raise SanitizerError(f"cannot back up {path}: {exc}") from exc
    return backup_path


def _restore_file(path: Path, backup_path: Path, metadata: os.stat_result) -> None:
    """Atomically restore a backup without following a replacement symlink."""

    try:
        os.replace(backup_path, path)
        restored = os.lstat(path)
        if (
            not stat.S_ISREG(restored.st_mode)
            or stat.S_IMODE(restored.st_mode) != stat.S_IMODE(metadata.st_mode)
            or restored.st_uid != metadata.st_uid
            or restored.st_gid != metadata.st_gid
        ):
            raise OSError("restored file has unexpected type or mode")
        directory_flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            directory_flags |= os.O_DIRECTORY
        directory_descriptor = os.open(path.parent, directory_flags)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except OSError as exc:
        raise SanitizerError(f"cannot restore {path} after failed patch: {exc}") from exc


def _verify_patch(
    before: DynamicInfo,
    after: DynamicInfo,
    *,
    tag: str,
    original_value: str,
    cleaned_value: str,
    path: Path,
) -> None:
    """Verify that only the selected search path changed."""

    if before.entries_without_search_paths() != after.entries_without_search_paths():
        raise SanitizerError(f"patchelf changed a non-RPATH/RUNPATH entry for {path}")

    if any(has_empty_component(value) for _tag, value in after.search_paths):
        raise SanitizerError(f"empty RPATH/RUNPATH component remains in {path}")

    expected_paths = () if not cleaned_value else ((tag, cleaned_value),)
    if after.search_paths != expected_paths:
        raise SanitizerError(
            f"unexpected RPATH/RUNPATH after patch for {path}: "
            f"expected {expected_paths!r}, got {after.search_paths!r}"
        )
    if not has_empty_component(original_value):
        raise SanitizerError(f"internal error: attempted to patch a clean search path in {path}")


def sanitize_file(path: Path, *, readelf: str, patchelf: str) -> bool:
    """Sanitize one ELF and return whether it was modified."""

    before = read_dynamic(path, readelf=readelf)
    if not before.search_paths:
        return False
    dirty_paths = tuple((tag, value) for tag, value in before.search_paths if has_empty_component(value))
    if not dirty_paths:
        return False
    if len(before.search_paths) != 1:
        # DT_RPATH and DT_RUNPATH are mutually exclusive in normal ELF files.
        # Refuse an unusual file rather than allowing a partial rewrite to
        # change which tag the loader observes.
        raise SanitizerError(f"multiple RPATH/RUNPATH entries are unsupported for {path}")

    tag, original_value = before.search_paths[0]
    cleaned_value = remove_empty_components(original_value)

    original_metadata = os.lstat(path)
    backup_path = _backup_file(path, original_metadata)
    if cleaned_value:
        command: list[str] = [patchelf]
        if tag == "RPATH":
            command.append("--force-rpath")
        command.extend(("--set-rpath", cleaned_value, str(path)))
    else:
        command = [patchelf, "--remove-rpath", str(path)]

    try:
        _run_tool(command, tool_name="patchelf")
        after = read_dynamic(path, readelf=readelf)
        _verify_patch(
            before,
            after,
            tag=tag,
            original_value=original_value,
            cleaned_value=cleaned_value,
            path=path,
        )
        patched_metadata = os.lstat(path)
        if (
            not stat.S_ISREG(patched_metadata.st_mode)
            or stat.S_IMODE(patched_metadata.st_mode) != stat.S_IMODE(original_metadata.st_mode)
            or patched_metadata.st_uid != original_metadata.st_uid
            or patched_metadata.st_gid != original_metadata.st_gid
        ):
            raise SanitizerError(f"patchelf changed file ownership or mode for {path}")
    except SanitizerError:
        _restore_file(path, backup_path, original_metadata)
        raise
    try:
        backup_path.unlink()
    except OSError as exc:
        raise SanitizerError(f"cannot remove backup {backup_path}: {exc}") from exc
    return True


def sanitize_tree(
    root: Path = Path("/"),
    *,
    readelf: str | None = None,
    patchelf: str | None = None,
) -> SanitizationReport:
    """Sanitize the fixed runtime targets below ``root``."""

    readelf_tool = _tool_path(readelf, "readelf")
    patchelf_tool = _tool_path(patchelf, "patchelf")
    modified: list[str] = []
    scanned = 0
    for path in iter_elf_files(root):
        scanned += 1
        if sanitize_file(path, readelf=readelf_tool, patchelf=patchelf_tool):
            modified.append(str(path))
    return SanitizationReport(scanned_elf_files=scanned, modified_files=tuple(modified))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path("/"),
        help="test-only rootfs prefix; targets remain /opt/conda and /app/comfyui",
    )
    parser.add_argument("--readelf", help="readelf executable (defaults to PATH lookup)")
    parser.add_argument("--patchelf", help="patchelf executable (defaults to PATH lookup)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        report = sanitize_tree(args.root, readelf=args.readelf, patchelf=args.patchelf)
    except (OSError, SanitizerError, ValueError) as exc:
        print(f"sanitize_elf_search_paths: error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report.as_dict(), sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
