#!/usr/bin/env python3
"""Generate and validate the slim launcher's portable system inventory.

The inventory is deliberately independent of Docker, GHCR, RunPod, and any
other image provider.  ``--root`` may point at a live root filesystem or at an
extracted image root.  The generator asks that root's ``ldconfig`` for the
actual cache entries and verifies the small set of paths used by the launcher.

The checked-in JSON is therefore evidence about the image that supplies the
launcher, plus any explicitly named libraries injected by the container
runtime (for example the NVIDIA driver ABI).  It is not a hand-maintained list
of libraries that an image might contain.  A missing path, a broken symlink,
an unusable executable, or an undeclared SONAME is an error.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
from pathlib import Path
import posixpath
import re
import subprocess
import sys
from typing import Mapping, Sequence


INVENTORY_KEYS = (
    "system_paths",
    "libraries",
    "library_paths",
    "executable_paths",
    "symlinks",
)

# These are the paths used before the materialized runtime is installed.  Keep
# this list small and explicit: adding a path changes the launcher contract and
# must be reflected by the actual slim image and by the checked-in inventory.
DEFAULT_CRITICAL_PATHS = (
    "/bin/bash",
    "/bin/sh",
    "/usr/bin/dash",
    "/usr/bin/dumb-init",
    "/usr/bin/env",
    "/lib64/ld-linux-x86-64.so.2",
)

_LDCONFIG_LINE = re.compile(r"^\s*(\S+)\s+\([^)]*\)\s+=>\s+(\S+)\s*$")


class InventoryError(RuntimeError):
    """Raised when an inventory cannot be generated or validated safely."""


def _root_path(root: Path, path: str) -> Path:
    if not path.startswith("/") or path == "/" or "\\" in path:
        raise InventoryError(f"unsafe absolute path: {path!r}")
    if any(part in ("", ".", "..") for part in path[1:].split("/")):
        raise InventoryError(f"unsafe absolute path: {path!r}")
    return root / path.lstrip("/")


def _normalise_link(source: str, target: str) -> str:
    if not target or "\\" in target:
        raise InventoryError(f"unsafe symlink target for {source}: {target!r}")
    if target.startswith("/"):
        result = posixpath.normpath(target)
    else:
        result = posixpath.normpath(posixpath.join(posixpath.dirname(source), target))
    if result == "/" or not result.startswith("/"):
        raise InventoryError(f"symlink escapes root: {source} -> {target}")
    if any(part in ("", ".", "..") for part in result[1:].split("/")):
        raise InventoryError(f"unsafe normalised symlink target: {source} -> {result}")
    return result


def _readlink_chain(
    root: Path,
    source: str,
    *,
    executable: bool,
    system_paths: set[str],
    executable_paths: set[str],
    symlinks: dict[str, str],
) -> None:
    """Validate a critical path and record every link in its resolution chain."""

    current = source
    visited: set[str] = set()
    while True:
        if current in visited:
            raise InventoryError(f"symlink cycle while resolving {source}")
        visited.add(current)
        system_paths.add(current)
        if executable:
            executable_paths.add(current)
        host_path = _root_path(root, current)
        try:
            metadata = host_path.lstat()
        except OSError as error:
            raise InventoryError(f"critical launcher path is missing: {current}") from error

        if host_path.is_symlink():
            try:
                target = _normalise_link(current, os.readlink(host_path))
            except OSError as error:
                raise InventoryError(f"cannot read launcher symlink: {current}") from error
            previous = symlinks.get(current)
            if previous is not None and previous != target:
                raise InventoryError(f"conflicting symlink target for {current}")
            symlinks[current] = target
            current = target
            continue

        if executable and not metadata.st_mode & 0o111:
            raise InventoryError(f"critical launcher path is not executable: {current}")
        return


def parse_ldconfig_output(output: str, *, root: Path) -> tuple[str, ...]:
    """Return real SONAMEs from ``ldconfig -p`` output in sorted order.

    ``ldconfig`` may print diagnostics before the cache table; only canonical
    cache rows are accepted.  Every accepted target must exist below ``root``
    so a stale cache cannot silently make the inventory claim a missing
    package.
    """

    names: set[str] = set()
    for line in output.splitlines():
        match = _LDCONFIG_LINE.match(line)
        if match is None:
            continue
        name, target = match.groups()
        if "/" in name or "\\" in name or name in (".", ".."):
            raise InventoryError(f"ldconfig returned an unsafe SONAME: {name!r}")
        if not target.startswith("/") or "\\" in target:
            raise InventoryError(f"ldconfig returned an unsafe library path: {target!r}")
        target_path = _root_path(root, target)
        try:
            target_path.stat()
        except OSError as error:
            raise InventoryError(f"ldconfig cache points at a missing library: {target}") from error
        names.add(name)
    if not names:
        raise InventoryError("ldconfig produced no usable SONAME entries")
    return tuple(sorted(names))


def _ldconfig_executable(root: Path, configured: str | None) -> str:
    if configured:
        return configured
    for candidate in ("/sbin/ldconfig", "/usr/sbin/ldconfig"):
        if _root_path(root, candidate).exists():
            return candidate
    return "ldconfig"


def read_ldconfig(root: Path, *, executable: str | None = None) -> tuple[str, ...]:
    """Read the linker cache belonging to ``root`` without entering it."""

    command = _ldconfig_executable(root, executable)
    try:
        result = subprocess.run(
            [command, "-p", "-r", str(root)],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
    except OSError as error:
        raise InventoryError(f"cannot execute ldconfig for {root}: {error}") from error
    if result.returncode != 0:
        diagnostics = result.stderr.strip()
        suffix = f": {diagnostics}" if diagnostics else ""
        raise InventoryError(f"ldconfig failed for {root} (status {result.returncode}){suffix}")
    return parse_ldconfig_output(result.stdout, root=root)


def build_inventory(
    root: Path,
    *,
    critical_paths: Sequence[str] = DEFAULT_CRITICAL_PATHS,
    ldconfig_names: Sequence[str] | None = None,
    ldconfig_executable: str | None = None,
    injected_libraries: Sequence[str] = (),
) -> dict[str, object]:
    """Build the canonical launcher inventory for a root filesystem."""

    try:
        root = root.resolve(strict=True)
    except OSError as error:
        raise InventoryError(f"root filesystem is unavailable: {root}") from error
    if not root.is_dir():
        raise InventoryError(f"root filesystem is not a directory: {root}")

    system_paths: set[str] = set()
    executable_paths: set[str] = set()
    symlinks: dict[str, str] = {}
    for path in sorted(set(critical_paths)):
        _root_path(root, path)
        _readlink_chain(
            root,
            path,
            executable=True,
            system_paths=system_paths,
            executable_paths=executable_paths,
            symlinks=symlinks,
        )

    libraries = tuple(ldconfig_names) if ldconfig_names is not None else read_ldconfig(root, executable=ldconfig_executable)
    for name in injected_libraries:
        if not name or "/" in name or "\\" in name or name in (".", ".."):
            raise InventoryError(f"unsafe injected SONAME: {name!r}")
    libraries = tuple(sorted(set(libraries).union(injected_libraries)))
    if tuple(sorted(set(libraries))) != tuple(libraries):
        raise InventoryError("ldconfig SONAMEs must be sorted and unique")
    return {
        "system_paths": sorted(system_paths),
        "libraries": list(libraries),
        "library_paths": [],
        "executable_paths": sorted(executable_paths),
        "symlinks": {path: symlinks[path] for path in sorted(symlinks)},
    }


def _validate_path_array(value: object, field: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise InventoryError(f"inventory {field} must be an array of strings")
    result = list(value)
    if result != sorted(set(result)):
        raise InventoryError(f"inventory {field} must be sorted and unique")
    for item in result:
        _root_path(Path("/"), item)
    return result


def validate_inventory_shape(value: object) -> dict[str, object]:
    """Validate and canonicalise checked-in inventory JSON."""

    if not isinstance(value, dict) or tuple(value) != INVENTORY_KEYS:
        raise InventoryError(
            "inventory keys must be exactly " + ", ".join(INVENTORY_KEYS) + " in canonical order"
        )
    system_paths = _validate_path_array(value["system_paths"], "system_paths")
    libraries = value["libraries"]
    if not isinstance(libraries, list) or any(not isinstance(item, str) for item in libraries):
        raise InventoryError("inventory libraries must be an array of strings")
    if libraries != sorted(set(libraries)):
        raise InventoryError("inventory libraries must be sorted and unique")
    for item in libraries:
        if not item or "/" in item or "\\" in item or item in (".", ".."):
            raise InventoryError(f"unsafe inventory SONAME: {item!r}")
    library_paths = _validate_path_array(value["library_paths"], "library_paths")
    executable_paths = _validate_path_array(value["executable_paths"], "executable_paths")
    raw_symlinks = value["symlinks"]
    if not isinstance(raw_symlinks, dict):
        raise InventoryError("inventory symlinks must be an object")
    symlinks: dict[str, str] = {}
    for source, target in raw_symlinks.items():
        if not isinstance(source, str) or not isinstance(target, str):
            raise InventoryError("inventory symlink paths must be strings")
        _root_path(Path("/"), source)
        _root_path(Path("/"), target)
        symlinks[source] = target
    if list(symlinks) != sorted(symlinks):
        raise InventoryError("inventory symlinks must be sorted by source path")
    return {
        "system_paths": system_paths,
        "libraries": list(libraries),
        "library_paths": library_paths,
        "executable_paths": executable_paths,
        "symlinks": symlinks,
    }


def validate_inventory_against_root(
    inventory: Mapping[str, object],
    root: Path,
    *,
    critical_paths: Sequence[str] = DEFAULT_CRITICAL_PATHS,
    ldconfig_executable: str | None = None,
    injected_libraries: Sequence[str] = (),
) -> None:
    """Fail if an inventory is stale, incomplete, or not reproducible."""

    actual = validate_inventory_shape(dict(inventory))
    expected = build_inventory(
        root,
        critical_paths=critical_paths,
        ldconfig_executable=ldconfig_executable,
        injected_libraries=injected_libraries,
    )
    if actual != expected:
        expected_text = render_inventory(expected).splitlines(keepends=True)
        actual_text = render_inventory(actual).splitlines(keepends=True)
        diff = "".join(
            difflib.unified_diff(
                actual_text,
                expected_text,
                fromfile="checked-in inventory",
                tofile="generated inventory",
            )
        )
        raise InventoryError("launcher inventory drifted from the actual root filesystem:\n" + diff)


def load_inventory(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InventoryError(f"launcher inventory JSON is invalid: {error}") from error
    return validate_inventory_shape(value)


def render_inventory(value: Mapping[str, object]) -> str:
    canonical = validate_inventory_shape(dict(value))
    return json.dumps(canonical, indent=2, ensure_ascii=False) + "\n"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/"), help="root filesystem to inspect (default: /)")
    parser.add_argument("--output", type=Path, help="write generated JSON here instead of stdout")
    parser.add_argument("--check", type=Path, help="validate this checked-in inventory against --root")
    parser.add_argument("--ldconfig", help="ldconfig executable to invoke")
    parser.add_argument(
        "--injected-library",
        action="append",
        default=[],
        help="SONAME supplied by the container runtime rather than the image (repeatable)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.check is not None and args.output is not None:
        print("--check and --output cannot be combined", file=sys.stderr)
        return 2
    try:
        if args.check is not None:
            inventory = load_inventory(args.check)
            validate_inventory_against_root(
                inventory,
                args.root,
                ldconfig_executable=args.ldconfig,
                injected_libraries=args.injected_library,
            )
            print(f"launcher inventory matches {args.root}")
            return 0
        generated = build_inventory(
            args.root,
            ldconfig_executable=args.ldconfig,
            injected_libraries=args.injected_library,
        )
        rendered = render_inventory(generated)
        if args.output is None:
            sys.stdout.write(rendered)
        else:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered, encoding="utf-8")
        return 0
    except InventoryError as error:
        print(f"runtime-launcher-inventory: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
