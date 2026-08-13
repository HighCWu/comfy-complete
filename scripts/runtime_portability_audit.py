#!/usr/bin/env python3
"""Read-only portability audit for a flattened ComfyComplete runtime.

The input is a materialized final rootfs, never an OCI image or image history.
The audit uses the same selection boundary as ``export_runtime.py`` and emits
stable JSON.  It does not build or pull images and does not contact Actions,
R2, RunPod, or any other service.
"""

from __future__ import annotations

import argparse
import json
import os
import posixpath
import re
import shlex
import shutil
import stat
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = 1
AUDIT_VERSION = "runtime-portability/v1"
DEFAULT_TARGETS = ("/opt/conda", "/app/comfyui")
DEFAULT_EXCLUDES = (
    "/app/comfyui/output",
    "/app/comfyui/temp",
    "/app/comfyui/models/_xdgcache",
    "/app/comfyui/models/_xdgconfig",
    "/app/comfyui/models/_xdgdata",
)
ALLOWED_TARGETS = frozenset(DEFAULT_TARGETS)
MUTABLE_PREFIXES = tuple(DEFAULT_EXCLUDES) + ("/runpod-volume",)
ELF_MAGIC = b"\x7fELF"
MAX_PATH = 4096
MAX_TEXT = 64 * 1024
MAX_SHEBANG = 4096
MAX_ENTRIES = 100_000
MAX_FINDINGS = 50_000
MAX_READELF_OUTPUT = 1_048_576
MAX_READelf_SECONDS = 5.0
MAX_SYMLINK_DEPTH = 64
SEVERITIES = ("blocker", "warning", "info")
SEVERITY_ORDER = {value: index for index, value in enumerate(SEVERITIES)}


class RuntimeAuditError(ValueError):
    """Raised when audit input is malformed or unsafe."""


@dataclass(frozen=True)
class Limits:
    max_entries: int = MAX_ENTRIES
    max_shebang_bytes: int = MAX_TEXT
    max_findings: int = MAX_FINDINGS
    readelf_timeout_seconds: float = MAX_READelf_SECONDS
    max_readelf_output_bytes: int = MAX_READELF_OUTPUT


@dataclass(frozen=True)
class Record:
    path: str
    host_path: Path
    kind: str
    mode: int
    size_bytes: int
    link_target: str | None = None


@dataclass(frozen=True)
class LauncherInventory:
    """Explicit paths and libraries provided by the minimal launcher image."""

    system_paths: tuple[str, ...] = ()
    libraries: tuple[str, ...] = ()
    library_paths: tuple[str, ...] = ()
    symlinks: tuple[tuple[str, str], ...] = ()
    executable_paths: tuple[str, ...] = ()

    @property
    def symlink_map(self) -> dict[str, str]:
        return dict(self.symlinks)

    @property
    def library_names(self) -> frozenset[str]:
        return frozenset(posixpath.basename(value) for value in self.libraries)


def _control(value: str) -> bool:
    return any(unicodedata.category(char).startswith("C") for char in value)


def _text(value: object, name: str, limit: int = MAX_PATH) -> str:
    if not isinstance(value, str) or not value or len(value) > limit or _control(value):
        raise RuntimeAuditError(f"{name} must be a bounded string without control characters")
    return value


def _absolute(value: object, name: str) -> str:
    path = _text(value, name)
    if "\\" in path or not path.startswith("/") or path == "/":
        raise RuntimeAuditError(f"{name} must be a traversal-free absolute POSIX path")
    if path.endswith("/"):
        path = path.rstrip("/")
    if any(part in ("", ".", "..") for part in path[1:].split("/")):
        raise RuntimeAuditError(f"{name} contains unsafe path components")
    return path


def _bundle(value: str) -> str:
    path = value.lstrip("/")
    if not path or len(path) > MAX_PATH or "\\" in path or _control(path):
        raise RuntimeAuditError(f"unsafe bundle path: {value!r}")
    if any(part in ("", ".", "..") for part in path.split("/")):
        raise RuntimeAuditError(f"unsafe bundle path: {value!r}")
    return path


def _prefix(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


def _policy(targets: Sequence[str], app_files: Sequence[str], excludes: Sequence[str]) -> dict[str, list[str]]:
    selected = sorted({_absolute(value, "target") for value in targets})
    if not selected or any(value not in ALLOWED_TARGETS for value in selected):
        raise RuntimeAuditError("targets must contain only /opt/conda and /app/comfyui")
    included = sorted({_absolute(value, "include-app") for value in app_files})
    if any(not value.startswith("/app/") for value in included):
        raise RuntimeAuditError("include-app paths must be under /app")
    excluded = sorted({_absolute(value, "exclude") for value in excludes})
    if any(not any(_prefix(value, target) for target in selected) for value in excluded):
        raise RuntimeAuditError("excludes must be inside selected targets")
    return {"targets": selected, "include_app": included, "excludes": excluded}


def load_selection_policy(path: Path) -> dict[str, list[str]]:
    """Read a selection_policy object or a complete exporter manifest."""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeAuditError(f"selection policy JSON is invalid: {error}") from error
    if isinstance(value, dict) and "selection_policy" in value:
        value = value["selection_policy"]
    if not isinstance(value, dict) or set(value) != {"targets", "include_app", "excludes"}:
        raise RuntimeAuditError("selection policy must contain targets, include_app, and excludes")
    if not all(isinstance(value[name], list) for name in ("targets", "include_app", "excludes")):
        raise RuntimeAuditError("selection policy fields must be arrays")
    return _policy(value["targets"], value["include_app"], value["excludes"])


def _library_names(values: object) -> tuple[str, ...]:
    if not isinstance(values, list):
        raise RuntimeAuditError("launcher libraries must be an array")
    result: set[str] = set()
    for value in values:
        item = _text(value, "launcher library")
        if "\\" in item or item.endswith("/") or item in (".", ".."):
            raise RuntimeAuditError("launcher library has unsafe syntax")
        if item.startswith("/"):
            _absolute(item, "launcher library")
        elif "/" in item:
            raise RuntimeAuditError("relative launcher library must be a basename")
        result.add(item)
    return tuple(sorted(result))


def load_launcher_inventory(path: Path) -> LauncherInventory:
    """Read explicit launcher-provided paths, libraries, and symlinks."""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeAuditError(f"launcher inventory JSON is invalid: {error}") from error
    if not isinstance(value, dict) or not set(value).issubset({"system_paths", "libraries", "library_paths", "symlinks", "executable_paths"}):
        raise RuntimeAuditError("launcher inventory has an invalid shape")

    def paths(name: str) -> tuple[str, ...]:
        values = value.get(name, [])
        if not isinstance(values, list):
            raise RuntimeAuditError(f"launcher inventory {name} must be an array")
        return tuple(sorted({_absolute(item, f"launcher {name}") for item in values}))

    raw_symlinks = value.get("symlinks", {})
    if not isinstance(raw_symlinks, dict):
        raise RuntimeAuditError("launcher inventory symlinks must be an object")
    symlinks: list[tuple[str, str]] = []
    for source, target in raw_symlinks.items():
        source_path = _absolute(source, "launcher symlink source")
        target_path = _absolute(target, "launcher symlink target")
        symlinks.append((source_path, target_path))
    return LauncherInventory(
        system_paths=paths("system_paths"),
        libraries=_library_names(value.get("libraries", [])),
        library_paths=paths("library_paths"),
        symlinks=tuple(sorted(symlinks)),
        executable_paths=paths("executable_paths"),
    )


def _root_path(root: Path, absolute_path: str) -> Path:
    path = root / _bundle(absolute_path)
    try:
        path.resolve(strict=False).relative_to(root)
    except ValueError as error:
        raise RuntimeAuditError(f"source path escapes root through symlink: {absolute_path}") from error
    return path


def _record(path: Path, relative: str) -> Record:
    try:
        info = path.lstat()
    except OSError as error:
        raise RuntimeAuditError(f"cannot stat {relative}: {error}") from error
    if stat.S_ISREG(info.st_mode):
        kind = "file"
    elif stat.S_ISDIR(info.st_mode):
        kind = "directory"
    elif stat.S_ISLNK(info.st_mode):
        kind = "symlink"
    else:
        kind = "special"
    target = os.readlink(path) if kind == "symlink" else None
    if target is not None:
        _text(target, f"symlink target {relative}")
        if "\\" in target:
            raise RuntimeAuditError(f"symlink target contains backslash: {relative}")
    return Record(relative, path, kind, stat.S_IMODE(info.st_mode), int(info.st_size), target)


def collect_records(root: Path, policy: Mapping[str, Sequence[str]], limits: Limits) -> tuple[list[Record], list[dict[str, Any]]]:
    """Collect selected entries without following symlinks."""

    try:
        resolved_root = root.resolve(strict=True)
    except OSError as error:
        raise RuntimeAuditError(f"source root is unavailable: {root}: {error}") from error
    if not resolved_root.is_dir():
        raise RuntimeAuditError(f"source root is not a directory: {root}")
    records: dict[str, Record] = {}
    findings: list[dict[str, Any]] = []

    def finding(code: str, severity: str, detail: str, path: str = "") -> None:
        findings.append({"code": code, "severity": severity, "path": path, "detail": detail})

    def collect(host_path: Path, relative: str) -> None:
        if any(_prefix("/" + relative, exclusion) for exclusion in policy["excludes"]):
            return
        if len(records) >= limits.max_entries:
            finding("entry_limit_exceeded", "blocker", "selected entry limit exceeded", relative)
            return
        try:
            item = _record(host_path, relative)
        except RuntimeAuditError as error:
            finding("unsafe_selected_path", "blocker", str(error), relative)
            return
        records[relative] = item
        if item.kind == "special":
            finding("special_file", "blocker", "special files are not portable runtime content", relative)
            return
        if item.kind != "directory":
            return
        try:
            children = sorted(host_path.iterdir(), key=lambda child: child.name)
        except OSError as error:
            finding("directory_unreadable", "blocker", str(error), relative)
            return
        for child in children:
            child_relative = f"{relative}/{child.name}"
            try:
                _bundle(child_relative)
            except RuntimeAuditError as error:
                finding("unsafe_selected_path", "blocker", str(error), child_relative)
                continue
            collect(child, child_relative)

    for target in policy["targets"]:
        relative = _bundle(target)
        host_path = _root_path(resolved_root, target)
        if not host_path.exists() or host_path.is_symlink() or not host_path.is_dir():
            finding("missing_target", "blocker", "selected target is not a real directory", relative)
            continue
        collect(host_path, relative)
    for included in policy["include_app"]:
        relative = _bundle(included)
        host_path = _root_path(resolved_root, included)
        if not host_path.exists() and not host_path.is_symlink():
            finding("missing_include_app", "blocker", "explicit include-app path does not exist", relative)
            continue
        if host_path.is_dir() and not host_path.is_symlink():
            finding("include_app_directory", "blocker", "include-app accepts files or symlinks, not directories", relative)
            continue
        collect(host_path, relative)
    return sorted(records.values(), key=lambda item: item.path), findings


def _resolve_selected(path: str, records: Mapping[str, Record]) -> tuple[str, Record | None]:
    current = path
    seen: set[str] = set()
    for _ in range(MAX_SYMLINK_DEPTH):
        item = records.get(current)
        if item is None:
            return "missing", None
        if item.kind != "symlink":
            return "ok", item
        if current in seen:
            return "cycle", None
        seen.add(current)
        if item.link_target is None:
            return "broken", None
        if item.link_target.startswith("/"):
            return "external", None
        current = posixpath.normpath(posixpath.join(posixpath.dirname(current), item.link_target))
        if current == "." or current.startswith("../"):
            return "external", None
    return "cycle", None


def _resolve_inventory(path: str, inventory: LauncherInventory, executable: bool = False) -> str:
    current = path
    seen: set[str] = set()
    mapping = inventory.symlink_map
    for _ in range(MAX_SYMLINK_DEPTH):
        if current in seen:
            return "cycle"
        seen.add(current)
        target = mapping.get(current)
        if target is None:
            if current not in inventory.system_paths:
                return "missing"
            if executable and current not in inventory.executable_paths:
                return "non_executable"
            return "provided"
        current = target
    return "cycle"


def _provision(path: str, records: Mapping[str, Record], inventory: LauncherInventory, executable: bool = False) -> str:
    status, item = _resolve_selected(path, records)
    if status == "ok" and item is not None:
        if executable and (item.kind != "file" or not item.mode & 0o111):
            return "non_executable"
        return "selected"
    if status in ("external", "cycle", "broken"):
        return status
    return _resolve_inventory(path, inventory, executable=executable)


def _add(findings: list[dict[str, Any]], code: str, severity: str, detail: str, path: str = "", evidence: Mapping[str, Any] | None = None) -> None:
    item: dict[str, Any] = {"code": code, "severity": severity, "path": path, "detail": detail}
    if evidence:
        item["evidence"] = dict(sorted(evidence.items()))
    findings.append(item)


def _read(path: Record, limit: int) -> bytes | None:
    try:
        with path.host_path.open("rb") as handle:
            return handle.read(limit)
    except OSError:
        return None


def _shebang(data: bytes) -> tuple[str, list[str]] | None:
    if not data.startswith(b"#!"):
        return None
    line = data.splitlines()[0][2:]
    try:
        value = line.decode("utf-8")
    except UnicodeDecodeError as error:
        raise RuntimeAuditError(f"shebang is not UTF-8: {error}") from error
    if not value or len(value) > MAX_SHEBANG or _control(value):
        raise RuntimeAuditError("shebang contains unsafe text")
    try:
        values = shlex.split(value)
    except ValueError as error:
        raise RuntimeAuditError(f"shebang cannot be parsed: {error}") from error
    return (values[0], values[1:]) if values else None


def check_shebangs(records: Sequence[Record], selected: Mapping[str, Record], inventory: LauncherInventory, findings: list[dict[str, Any]], required_paths: set[str], limits: Limits) -> int:
    count = 0
    for item in records:
        if item.kind != "file" or not item.mode & 0o111:
            continue
        data = _read(item, limits.max_shebang_bytes)
        if data is None:
            continue
        try:
            parsed = _shebang(data)
        except RuntimeAuditError as error:
            _add(findings, "unsafe_shebang", "blocker", str(error), item.path)
            continue
        if parsed is None:
            continue
        count += 1
        interpreter, _ = parsed
        if interpreter == "/usr/bin/env":
            status = _provision(interpreter, selected, inventory, executable=True)
            if status in ("selected", "provided"):
                _add(findings, "path_dependent_shebang", "warning", "env shebang depends on launcher PATH", item.path)
            else:
                required_paths.add(interpreter)
                _add(findings, "missing_env_interpreter", "blocker", f"/usr/bin/env is not provided as an executable ({status})", item.path, {"interpreter": interpreter})
            continue
        if not interpreter.startswith("/"):
            _add(findings, "relative_shebang", "warning", "interpreter depends on launcher PATH", item.path, {"interpreter": interpreter})
            continue
        status = _provision(interpreter, selected, inventory, executable=True)
        if status in ("selected", "provided"):
            _add(findings, "absolute_shebang_resolved", "info", f"absolute interpreter is provided by {status}", item.path, {"interpreter": interpreter})
        else:
            required_paths.add(interpreter)
            _add(findings, "missing_absolute_shebang", "blocker", f"absolute interpreter is not provided ({status})", item.path, {"interpreter": interpreter})
    return count


def _readelf(path: Record, limits: Limits) -> tuple[str, bool] | None:
    executable = shutil.which("readelf")
    if executable is None:
        return None
    try:
        result = subprocess.run([executable, "-lW", "-dW", str(path.host_path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=limits.readelf_timeout_seconds, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or len(result.stdout) > limits.max_readelf_output_bytes:
        return None
    try:
        output = result.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if _control(output.replace("\n", "").replace("\t", "")):
        return None
    return output, True


def _elf_metadata(output: str) -> dict[str, Any]:
    interpreter = re.search(r"Requesting program interpreter:\s*([^\]]*)\]", output)
    interpreter_value = interpreter.group(1).strip() if interpreter else None
    if interpreter_value is not None and (
        not interpreter_value or len(interpreter_value) > MAX_PATH or _control(interpreter_value)
    ):
        raise RuntimeAuditError("readelf metadata contains unsafe interpreter text")

    needed: list[str] = []
    for value in re.findall(r"Shared library:\s*\[([^\]]*)\]", output):
        value = value.strip()
        if not value or len(value) > MAX_PATH or _control(value):
            raise RuntimeAuditError("readelf metadata contains unsafe library text")
        needed.append(value)

    def optional_search_paths(pattern: str) -> list[str]:
        values: list[str] = []
        for value in re.findall(pattern, output):
            value = value.strip()
            # readelf prints `[]` for an empty RPATH/RUNPATH. That means no
            # search path and must not become a synthetic unresolved entry.
            if not value:
                continue
            if len(value) > MAX_PATH or _control(value):
                raise RuntimeAuditError("readelf metadata contains unsafe search-path text")
            values.append(value)
        return sorted(set(values))

    rpaths = optional_search_paths(r"\(RPATH\).*?Library rpath:\s*\[([^\]]*)\]")
    runpaths = optional_search_paths(r"\(RUNPATH\).*?Library runpath:\s*\[([^\]]*)\]")
    return {
        "interpreter": interpreter_value,
        "needed": sorted(set(needed)),
        "rpath": rpaths,
        "runpath": runpaths,
    }


def _origin_path(raw: str, item: Record) -> str | None:
    if "$ORIGIN" in raw or "${ORIGIN}" in raw:
        raw = raw.replace("${ORIGIN}", posixpath.dirname("/" + item.path)).replace("$ORIGIN", posixpath.dirname("/" + item.path))
    if not raw.startswith("/") or "\\" in raw or _control(raw):
        return None
    normalized = posixpath.normpath(raw)
    if normalized == "/" or normalized == "/.." or normalized.startswith("/../"):
        return None
    return normalized


def _library_candidates(name: str, item: Record, metadata: Mapping[str, Any], selected: Mapping[str, Record], inventory: LauncherInventory) -> list[str]:
    paths: list[str] = []
    for raw in [*metadata["rpath"], *metadata["runpath"]]:
        for component in raw.split(":"):
            directory = _origin_path(component, item)
            if directory:
                candidate = posixpath.join(directory, name)
                if candidate in selected and candidate not in paths:
                    paths.append(candidate)
    return paths


def check_elfs(records: Sequence[Record], selected: Mapping[str, Record], inventory: LauncherInventory, findings: list[dict[str, Any]], required_paths: set[str], required_libraries: set[str], required_library_paths: set[str], limits: Limits) -> tuple[int, list[dict[str, Any]], bool]:
    count = 0
    reports: list[dict[str, Any]] = []
    limited = False
    for item in records:
        if item.kind != "file" or _read(item, 4) != ELF_MAGIC:
            continue
        count += 1
        result = _readelf(item, limits)
        if result is None:
            limited = True
            _add(findings, "readelf_unavailable_or_failed", "warning", "ELF metadata could not be inspected; audit is limited", item.path)
            continue
        try:
            metadata = _elf_metadata(result[0])
        except RuntimeAuditError as error:
            # A single malformed or tool-hostile readelf record must not
            # discard the report for every other selected ELF. Keep the audit
            # fail-closed, but make the limitation visible in JSON and let the
            # caller inspect the offending path.
            limited = True
            _add(findings, "readelf_metadata_invalid", "warning", str(error), item.path)
            continue
        reports.append({"path": item.path, **metadata})
        interpreter = metadata["interpreter"]
        if interpreter:
            status = _provision(interpreter, selected, inventory, executable=True)
            if status not in ("selected", "provided"):
                required_paths.add(interpreter)
                _add(findings, "missing_elf_interpreter", "blocker", f"PT_INTERP is not provided ({status})", item.path, {"interpreter": interpreter})
            else:
                _add(findings, "elf_interpreter_resolved", "info", f"PT_INTERP is provided by {status}", item.path, {"interpreter": interpreter})
        for name in metadata["needed"]:
            if name.startswith("/"):
                status = _provision(name, selected, inventory)
                if status not in ("selected", "provided"):
                    required_paths.add(name)
                    _add(findings, "missing_absolute_elf_library", "blocker", f"absolute DT_NEEDED path is not provided ({status})", item.path, {"needed": name})
                continue
            candidates = _library_candidates(name, item, metadata, selected, inventory)
            if candidates or name in inventory.library_names:
                _add(findings, "elf_library_resolved", "info", "DT_NEEDED is provided by selected runtime or launcher inventory", item.path, {"needed": name, "resolved": candidates[0] if candidates else name})
            else:
                required_libraries.add(name)
                _add(findings, "missing_elf_library", "blocker", "DT_NEEDED is absent from selected runtime and launcher inventory", item.path, {"needed": name})
        for kind in ("rpath", "runpath"):
            for raw in metadata[kind]:
                for component in raw.split(":"):
                    directory = _origin_path(component, item)
                    if directory is None:
                        required_library_paths.add(component)
                        _add(findings, "unresolved_elf_search_path", "blocker", f"{kind.upper()} component is not absolute or $ORIGIN-relative", item.path, {kind: component})
                    elif any(_prefix(directory, prefix) for prefix in MUTABLE_PREFIXES):
                        _add(findings, "elf_search_path_mutable", "blocker", f"{kind.upper()} points into mutable or excluded state", item.path, {kind: directory})
                    elif directory not in selected and not any(_prefix(directory, value) for value in inventory.library_paths):
                        required_library_paths.add(directory)
                        _add(findings, "missing_elf_search_path", "blocker", f"{kind.upper()} directory is not provided", item.path, {kind: directory})
                    else:
                        _add(findings, "elf_search_path_resolved", "info", f"{kind.upper()} directory is provided", item.path, {kind: directory})
    return count, sorted(reports, key=lambda value: value["path"]), limited


def symlink_stats(records: Sequence[Record], selected: Mapping[str, Record], findings: list[dict[str, Any]]) -> dict[str, int]:
    stats = {"total": 0, "valid": 0, "broken": 0, "external": 0, "cycles": 0}
    for item in records:
        if item.kind != "symlink":
            continue
        stats["total"] += 1
        status, _ = _resolve_selected("/" + item.path, selected)
        if status == "ok":
            stats["valid"] += 1
        else:
            key = "cycles" if status == "cycle" else "external" if status == "external" else "broken"
            stats[key] += 1
            _add(findings, "symlink_" + key, "blocker", "symlink is not a valid selected-runtime link", item.path)
    return stats


def _sorted_findings(findings: Sequence[dict[str, Any]], limit: int) -> tuple[list[dict[str, Any]], bool]:
    unique = {json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")): value for value in findings}
    ordered = sorted(unique.values(), key=lambda value: (SEVERITY_ORDER[value["severity"]], value["code"], value["path"], value["detail"]))
    return ordered[:limit], len(ordered) > limit


def audit_runtime(*, source_root: Path, targets: Sequence[str] = DEFAULT_TARGETS, app_files: Sequence[str] = (), exclusions: Sequence[str] = DEFAULT_EXCLUDES, launcher_inventory: LauncherInventory | None = None, limits: Limits | None = None) -> dict[str, Any]:
    active_limits = limits or Limits()
    if active_limits.max_entries <= 0 or active_limits.max_findings <= 0 or active_limits.max_shebang_bytes <= 0:
        raise RuntimeAuditError("audit limits must be positive")
    policy = _policy(targets, app_files, exclusions)
    inventory = launcher_inventory or LauncherInventory()
    records, findings = collect_records(source_root, policy, active_limits)
    selected = {"/" + item.path: item for item in records}
    required_paths: set[str] = set()
    required_libraries: set[str] = set()
    required_library_paths: set[str] = set()
    scripts = check_shebangs(records, selected, inventory, findings, required_paths, active_limits)
    elf_count, elf_reports, elf_limited = check_elfs(records, selected, inventory, findings, required_paths, required_libraries, required_library_paths, active_limits)
    links = symlink_stats(records, selected, findings)
    findings, findings_truncated = _sorted_findings(findings, active_limits.max_findings)
    limited = elf_limited or findings_truncated
    if findings_truncated:
        findings.append({"code": "finding_limit_exceeded", "severity": "blocker", "path": "", "detail": "finding limit exceeded; report is incomplete"})
    counts = {severity: sum(1 for value in findings if value["severity"] == severity) for severity in SEVERITIES}
    status = "blocker" if counts["blocker"] else "limited" if limited else "pass"
    return {
        "schema_version": SCHEMA_VERSION,
        "audit_version": AUDIT_VERSION,
        "status": status,
        "selection_policy": policy,
        "tooling": {"readelf_available": shutil.which("readelf") is not None, "elf_analysis": "limited" if elf_limited else "complete"},
        "summary": {"selected_entries": len(records), "regular_files": sum(value.kind == "file" for value in records), "directories": sum(value.kind == "directory" for value in records), "scripts": scripts, "elf_files": elf_count, "finding_counts": counts},
        "symlinks": links,
        "elf": {"files": elf_reports},
        "findings": findings,
        "launcher_image_requirements": {"system_paths": sorted(required_paths), "libraries": sorted(required_libraries), "library_search_paths": sorted(required_library_paths)},
        "provided_launcher_inventory": {"system_paths": list(inventory.system_paths), "libraries": list(inventory.libraries), "library_paths": list(inventory.library_paths), "symlinks": [{"path": source, "target": target} for source, target in inventory.symlinks]},
    }


def render_report(report: Mapping[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--selection-policy", type=Path)
    parser.add_argument("--target", action="append", dest="targets")
    parser.add_argument("--include-app", action="append", default=[])
    parser.add_argument("--exclude", action="append", dest="excludes")
    parser.add_argument("--launcher-inventory", type=Path)
    parser.add_argument("--max-entries", type=int, default=MAX_ENTRIES)
    parser.add_argument("--max-shebang-bytes", type=int, default=MAX_TEXT)
    parser.add_argument("--max-findings", type=int, default=MAX_FINDINGS)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        policy = load_selection_policy(args.selection_policy) if args.selection_policy else _policy(args.targets or DEFAULT_TARGETS, args.include_app, args.excludes or DEFAULT_EXCLUDES)
        inventory = load_launcher_inventory(args.launcher_inventory) if args.launcher_inventory else None
        report = audit_runtime(source_root=args.source_root, targets=policy["targets"], app_files=policy["include_app"], exclusions=policy["excludes"], launcher_inventory=inventory, limits=Limits(args.max_entries, args.max_shebang_bytes, args.max_findings))
        rendered = render_report(report)
        if args.output:
            args.output.write_text(rendered, encoding="utf-8")
        else:
            sys.stdout.write(rendered)
        return 1 if report["status"] in ("blocker", "limited") else 0
    except (RuntimeAuditError, OSError) as error:
        print(f"runtime portability audit failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
