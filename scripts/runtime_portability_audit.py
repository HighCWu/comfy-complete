#!/usr/bin/env python3
"""Read-only portability audit for a flattened ComfyComplete runtime.

The input is a materialized final rootfs, never an OCI image or image history.
The audit uses the same selection boundary as ``export_runtime.py`` and emits
stable JSON.  It does not build or pull images and does not contact Actions,
R2, RunPod, or any other service.
"""

from __future__ import annotations

import argparse
import hashlib
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


SCHEMA_VERSION = 2
AUDIT_VERSION = "runtime-portability/v2"
CRITICAL_CONFIG_VERSION = 3
CRITICAL_PROBE_VERSION = 3
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
MAX_ENTRIES = 500_000
MAX_FINDINGS = 50_000
MAX_READELF_OUTPUT = 1_048_576
MAX_READelf_SECONDS = 5.0
MAX_SYMLINK_DEPTH = 64
MAX_CRITICAL_ELFS = 256
MAX_CRITICAL_PROBE_OUTPUT = 256 * 1024
MAX_CRITICAL_PATHS = 10_000
MAX_CRITICAL_SOURCE_BYTES = 4 * 1024 * 1024
CRITICAL_IMPORT_ALLOWLIST = frozenset(
    {"torch", "numpy", "PIL", "aiohttp", "folder_paths", "comfy", "server", "execution"}
)
CRITICAL_PROFILES = ("cpu", "gpu_required")
CRITICAL_IMPORT_PROFILES = frozenset(CRITICAL_PROFILES)
CRITICAL_PROFILE_IMPORTS = {
    "cpu": frozenset({"cpu"}),
    "gpu_required": frozenset({"cpu", "gpu_required"}),
}
CRITICAL_IMPORT_STATUSES = frozenset({"pass", "failed", "timeout", "not_executed"})
CRITICAL_SKIP_REASON = "environment_unavailable"
CRITICAL_MODULE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")
CRITICAL_POLICY = {
    "network": "none",
    "rootfs": "read-only",
    "writable_paths": ["/tmp", "/audit"],
    "execution": "allowlisted-interpreter-only",
    "gpu": "CUDA_VISIBLE_DEVICES-empty-no-explicit-init",
}
CRITICAL_PROFILE_POLICIES = {
    "cpu": dict(CRITICAL_POLICY),
    "gpu_required": {
        **CRITICAL_POLICY,
        "gpu": "CUDA_VISIBLE_DEVICES-provided-gpu-smoke",
    },
}
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


def _critical_module(value: object, name: str) -> str:
    module = _text(value, name, limit=256)
    if not CRITICAL_MODULE_RE.fullmatch(module) or module not in CRITICAL_IMPORT_ALLOWLIST:
        raise RuntimeAuditError(f"{name} is not an allowlisted critical import")
    return module


def validate_critical_config(value: object) -> dict[str, Any]:
    """Validate the immutable, repository-owned base critical-runtime contract."""

    if not isinstance(value, dict):
        raise RuntimeAuditError("critical runtime config must be an object")
    required = {
        "schema_version", "profile", "probe_profiles", "default_probe_profile",
        "probe_policy", "interpreter", "working_directory", "main_script",
        "entrypoints", "launcher_contract", "imports", "import_review",
    }
    if set(value) != required:
        raise RuntimeAuditError("critical runtime config has an invalid shape")
    if value["schema_version"] != CRITICAL_CONFIG_VERSION or value["profile"] != "base":
        raise RuntimeAuditError("unsupported critical runtime config version or profile")
    probe_profiles = value["probe_profiles"]
    if (
        not isinstance(probe_profiles, list)
        or probe_profiles != list(CRITICAL_PROFILES)
        or len(set(probe_profiles)) != len(probe_profiles)
    ):
        raise RuntimeAuditError("critical probe_profiles must declare cpu and gpu_required exactly once")
    default_probe_profile = value["default_probe_profile"]
    if default_probe_profile not in CRITICAL_IMPORT_PROFILES:
        raise RuntimeAuditError("critical default_probe_profile is invalid")
    probe_policy = value["probe_policy"]
    if probe_policy != CRITICAL_POLICY:
        raise RuntimeAuditError("critical probe policy is invalid")
    interpreter = _absolute(value["interpreter"], "critical interpreter")
    working_directory = _absolute(value["working_directory"], "critical working directory")
    main_script = _absolute(value["main_script"], "critical main script")
    if interpreter != "/opt/conda/bin/python":
        raise RuntimeAuditError("base critical interpreter must be /opt/conda/bin/python")
    if working_directory != "/app/comfyui" or main_script != "/app/comfyui/main.py":
        raise RuntimeAuditError("base critical working directory and main script are fixed")

    raw_entrypoints = value["entrypoints"]
    if not isinstance(raw_entrypoints, list) or not raw_entrypoints:
        raise RuntimeAuditError("critical entrypoints must be a non-empty array")
    entrypoints: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for index, raw in enumerate(raw_entrypoints):
        if not isinstance(raw, dict) or set(raw) != {"path", "owner", "kind", "required_executable", "shebang"}:
            raise RuntimeAuditError(f"critical entrypoints[{index}] has an invalid shape")
        path = _absolute(raw["path"], f"critical entrypoints[{index}].path")
        if path in seen_paths:
            raise RuntimeAuditError(f"duplicate critical entrypoint: {path}")
        seen_paths.add(path)
        owner = raw["owner"]
        kind = raw["kind"]
        if owner not in {"launcher", "runtime"} or kind not in {"script", "elf", "python"}:
            raise RuntimeAuditError(f"critical entrypoints[{index}] owner or kind is invalid")
        if not isinstance(raw["required_executable"], bool):
            raise RuntimeAuditError(f"critical entrypoints[{index}].required_executable must be boolean")
        shebang = raw["shebang"]
        if kind == "script":
            if not isinstance(shebang, str):
                raise RuntimeAuditError(f"critical entrypoints[{index}].shebang is required for scripts")
            shebang = _absolute(shebang, f"critical entrypoints[{index}].shebang")
        elif shebang is not None:
            raise RuntimeAuditError(f"critical entrypoints[{index}].shebang is only valid for scripts")
        if not (path.startswith("/app/") or path.startswith("/opt/conda/")):
            raise RuntimeAuditError(f"critical entrypoints[{index}].path is outside the allowed image contract")
        entrypoints.append({"path": path, "owner": owner, "kind": kind, "required_executable": raw["required_executable"], "shebang": shebang})

    if interpreter not in seen_paths or main_script not in seen_paths:
        raise RuntimeAuditError("critical entrypoints must include the configured interpreter and main script")
    by_path = {item["path"]: item for item in entrypoints}
    if by_path[interpreter] != {
        "path": interpreter,
        "owner": "runtime",
        "kind": "elf",
        "required_executable": True,
        "shebang": None,
    }:
        raise RuntimeAuditError("base critical interpreter entrypoint contract is invalid")
    if by_path[main_script] != {
        "path": main_script,
        "owner": "runtime",
        "kind": "python",
        "required_executable": False,
        "shebang": None,
    }:
        raise RuntimeAuditError("base critical main-script entrypoint contract is invalid")
    if any(item["owner"] == "launcher" for item in entrypoints):
        raise RuntimeAuditError("launcher-owned paths must be declared in launcher_contract, not entrypoints")

    raw_launcher_contract = value["launcher_contract"]
    if not isinstance(raw_launcher_contract, list) or not raw_launcher_contract:
        raise RuntimeAuditError("critical launcher_contract must be a non-empty array")
    launcher_contract: list[dict[str, Any]] = []
    contract_paths: set[str] = set()
    for index, raw in enumerate(raw_launcher_contract):
        if not isinstance(raw, dict) or set(raw) != {"path", "kind", "required_executable", "shebang"}:
            raise RuntimeAuditError(f"critical launcher_contract[{index}] has an invalid shape")
        path = _absolute(raw["path"], f"critical launcher_contract[{index}].path")
        kind = raw["kind"]
        if kind not in {"script", "elf"} or not isinstance(raw["required_executable"], bool):
            raise RuntimeAuditError(f"critical launcher_contract[{index}] has invalid kind or executable flag")
        shebang = raw["shebang"]
        if kind == "script":
            shebang = _absolute(shebang, f"critical launcher_contract[{index}].shebang")
        elif shebang is not None:
            raise RuntimeAuditError(f"critical launcher_contract[{index}].shebang is only valid for scripts")
        if path in contract_paths:
            raise RuntimeAuditError(f"duplicate critical contract path: {path}")
        contract_paths.add(path)
        launcher_contract.append({"path": path, "kind": kind, "required_executable": raw["required_executable"], "shebang": shebang})

    raw_imports = value["imports"]
    if not isinstance(raw_imports, list) or not raw_imports:
        raise RuntimeAuditError("critical imports must be a non-empty array")
    imports: list[dict[str, Any]] = []
    seen_modules: set[str] = set()
    for index, raw in enumerate(raw_imports):
        if not isinstance(raw, dict) or set(raw) != {"module", "required", "profile"}:
            raise RuntimeAuditError(f"critical imports[{index}] has an invalid shape")
        module = _critical_module(raw["module"], f"critical imports[{index}].module")
        if module in seen_modules:
            raise RuntimeAuditError(f"duplicate critical import: {module}")
        if not isinstance(raw["required"], bool):
            raise RuntimeAuditError(f"critical imports[{index}].required must be boolean")
        if raw["profile"] not in CRITICAL_IMPORT_PROFILES:
            raise RuntimeAuditError(f"critical imports[{index}].profile is invalid")
        seen_modules.add(module)
        imports.append({"module": module, "required": raw["required"], "profile": raw["profile"]})
    raw_review = value["import_review"]
    if not isinstance(raw_review, list) or len(raw_review) != len(imports):
        raise RuntimeAuditError("critical import_review must contain one entry per import")
    review: list[dict[str, Any]] = []
    review_modules: set[str] = set()
    for index, raw in enumerate(raw_review):
        if not isinstance(raw, dict) or set(raw) != {"module", "safe_import", "profile", "reason"}:
            raise RuntimeAuditError(f"critical import_review[{index}] has an invalid shape")
        module = _critical_module(raw["module"], f"critical import_review[{index}].module")
        if module in review_modules or module not in seen_modules:
            raise RuntimeAuditError(f"critical import_review[{index}] does not match imports")
        if not isinstance(raw["safe_import"], bool) or not raw["safe_import"]:
            raise RuntimeAuditError(f"critical import_review[{index}].safe_import must be true")
        if raw["profile"] not in CRITICAL_IMPORT_PROFILES:
            raise RuntimeAuditError(f"critical import_review[{index}].profile is invalid")
        expected_profile = next(item["profile"] for item in imports if item["module"] == module)
        if raw["profile"] != expected_profile:
            raise RuntimeAuditError(f"critical import_review[{index}].profile does not match imports")
        reason = _text(raw["reason"], f"critical import_review[{index}].reason", limit=MAX_CRITICAL_PROBE_OUTPUT)
        review_modules.add(module)
        review.append({"module": module, "safe_import": True, "profile": raw["profile"], "reason": reason})
    if review_modules != seen_modules:
        raise RuntimeAuditError("critical import_review must exactly match imports")
    return {
        "schema_version": CRITICAL_CONFIG_VERSION,
        "profile": "base",
        "probe_profiles": list(CRITICAL_PROFILES),
        "default_probe_profile": default_probe_profile,
        "probe_policy": dict(CRITICAL_POLICY),
        "interpreter": interpreter,
        "working_directory": working_directory,
        "main_script": main_script,
        "entrypoints": entrypoints,
        "launcher_contract": launcher_contract,
        "imports": imports,
        "import_review": review,
    }


def load_critical_config(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeAuditError(f"critical runtime config JSON is invalid: {error}") from error
    return validate_critical_config(value)


def _critical_path(value: object, name: str) -> str:
    path = _text(value, name, limit=MAX_PATH)
    if not path.startswith("/") or "\\" in path or any(part in ("", ".", "..") for part in path[1:].split("/")):
        raise RuntimeAuditError(f"{name} must be a normalized absolute POSIX path")
    return path


def load_critical_probe(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeAuditError(f"critical probe JSON is invalid: {error}") from error
    required_report_fields = {
        "schema_version", "profile", "probe_profile", "config_sha256", "status",
        "coverage", "policy", "main_script_compile", "import_review", "imports",
    }
    allowed_report_fields = required_report_fields | {"error"}
    if not isinstance(value, dict) or not set(value).issubset(allowed_report_fields) or not required_report_fields.issubset(value):
        raise RuntimeAuditError("critical probe report has an invalid shape")
    if value["schema_version"] != CRITICAL_PROBE_VERSION or value["profile"] != "base":
        raise RuntimeAuditError("unsupported critical probe version or profile")
    if value["probe_profile"] not in CRITICAL_IMPORT_PROFILES:
        raise RuntimeAuditError("critical probe profile is invalid")
    digest = value["config_sha256"]
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise RuntimeAuditError("critical probe config_sha256 is invalid")
    if value["status"] not in {"pass", "partial", "blocker", "limited"}:
        raise RuntimeAuditError("critical probe status is invalid")
    if value["coverage"] not in {"complete", "partial", "incomplete"}:
        raise RuntimeAuditError("critical probe coverage is invalid")
    if "error" in value and (not isinstance(value["error"], str) or len(value["error"]) > MAX_CRITICAL_PROBE_OUTPUT or _control(value["error"])):
        raise RuntimeAuditError("critical probe error is invalid")
    compile_report = value["main_script_compile"]
    if not isinstance(compile_report, dict) or set(compile_report) - {"path", "status", "source_bytes", "error"} or not {"path", "status", "source_bytes"}.issubset(compile_report):
        raise RuntimeAuditError("critical main-script compile report is invalid")
    _critical_path(compile_report["path"], "critical main-script compile path")
    if compile_report["status"] not in {"pass", "failed"} or not isinstance(compile_report["source_bytes"], int) or not 0 <= compile_report["source_bytes"] <= MAX_CRITICAL_SOURCE_BYTES:
        raise RuntimeAuditError("critical main-script compile metadata is invalid")
    if "error" in compile_report and (not isinstance(compile_report["error"], str) or len(compile_report["error"]) > MAX_CRITICAL_PROBE_OUTPUT or _control(compile_report["error"])):
        raise RuntimeAuditError("critical main-script compile error is invalid")
    import_review = value["import_review"]
    if not isinstance(import_review, list) or len(import_review) > MAX_CRITICAL_PATHS:
        raise RuntimeAuditError("critical import_review is invalid")
    review_modules: set[str] = set()
    for index, item in enumerate(import_review):
        if not isinstance(item, dict) or set(item) != {"module", "safe_import", "profile", "reason"}:
            raise RuntimeAuditError(f"critical probe import_review[{index}] is invalid")
        module = _critical_module(item["module"], f"critical probe import_review[{index}].module")
        if (
            module in review_modules
            or not item["safe_import"]
            or item["profile"] not in CRITICAL_IMPORT_PROFILES
            or not isinstance(item["reason"], str)
            or not item["reason"]
        ):
            raise RuntimeAuditError(f"critical probe import_review[{index}] is invalid")
        review_modules.add(module)
    policy = value["policy"]
    expected_policy = CRITICAL_PROFILE_POLICIES[value["probe_profile"]]
    if not isinstance(policy, dict) or policy != expected_policy:
        raise RuntimeAuditError("critical probe policy is invalid")
    imports = value["imports"]
    if not isinstance(imports, list) or len(imports) > MAX_CRITICAL_PATHS:
        raise RuntimeAuditError("critical probe imports must be an array")
    seen_modules: set[str] = set()
    for index, item in enumerate(imports):
        required_fields = {
            "module", "required", "profile", "status", "duration_ms",
            "before_shared_objects", "before_shared_object_classification", "before_mapped_files",
            "cumulative_shared_objects", "cumulative_shared_object_classification", "cumulative_mapped_files",
            "new_shared_objects", "new_shared_object_classification", "new_mapped_files",
            "stderr", "stdout",
        }
        if not isinstance(item, dict) or not set(item).issubset(required_fields | {"reason_code"}) or not required_fields.issubset(item):
            raise RuntimeAuditError(f"critical probe imports[{index}] has an invalid shape")
        module = _critical_module(item["module"], f"critical probe imports[{index}].module")
        if module in seen_modules:
            raise RuntimeAuditError(f"duplicate critical probe import: {module}")
        seen_modules.add(module)
        if not isinstance(item["required"], bool) or item["profile"] not in CRITICAL_IMPORT_PROFILES or item["status"] not in CRITICAL_IMPORT_STATUSES:
            raise RuntimeAuditError(f"critical probe imports[{index}] has invalid status metadata")
        if item["status"] == "not_executed":
            if item["profile"] != "gpu_required" or item.get("reason_code") != CRITICAL_SKIP_REASON:
                raise RuntimeAuditError(f"critical probe imports[{index}] has an invalid not_executed reason")
        elif "reason_code" in item:
            raise RuntimeAuditError(f"critical probe imports[{index}] has an unexpected reason_code")
        if not isinstance(item["duration_ms"], int) or item["duration_ms"] < 0:
            raise RuntimeAuditError(f"critical probe imports[{index}].duration_ms is invalid")
        for field in ("before_shared_objects", "cumulative_shared_objects", "new_shared_objects", "before_mapped_files", "cumulative_mapped_files", "new_mapped_files"):
            values = item[field]
            if not isinstance(values, list) or len(values) > MAX_CRITICAL_PATHS or values != sorted(set(values)):
                raise RuntimeAuditError(f"critical probe imports[{index}].{field} is invalid")
            for path in values:
                _critical_path(path, f"critical probe imports[{index}].{field}")
        if not set(item["before_shared_objects"]).issubset(item["before_mapped_files"]):
            raise RuntimeAuditError(f"critical probe before shared objects are not mapped at imports[{index}]")
        if not set(item["cumulative_shared_objects"]).issubset(item["cumulative_mapped_files"]):
            raise RuntimeAuditError(f"critical probe cumulative shared objects are not mapped at imports[{index}]")
        if not set(item["new_shared_objects"]).issubset(item["cumulative_shared_objects"]):
            raise RuntimeAuditError(f"critical probe new shared objects are not cumulative at imports[{index}]")
        if not set(item["new_mapped_files"]).issubset(item["cumulative_mapped_files"]):
            raise RuntimeAuditError(f"critical probe new mappings are not cumulative at imports[{index}]")
        for classification_field, objects_field in (
            ("before_shared_object_classification", "before_shared_objects"),
            ("cumulative_shared_object_classification", "cumulative_shared_objects"),
            ("new_shared_object_classification", "new_shared_objects"),
        ):
            classification = item[classification_field]
            if not isinstance(classification, dict) or set(classification) != {"runtime", "launcher_or_system", "other"}:
                raise RuntimeAuditError(f"critical probe {classification_field} is invalid at imports[{index}]")
            flattened: list[str] = []
            for paths in classification.values():
                if not isinstance(paths, list) or len(paths) > MAX_CRITICAL_PATHS or paths != sorted(set(paths)):
                    raise RuntimeAuditError(f"critical probe {classification_field} paths are invalid at imports[{index}]")
                for path in paths:
                    _critical_path(path, f"critical probe {classification_field} path at imports[{index}]")
                flattened.extend(paths)
            if set(flattened) != set(item[objects_field]):
                raise RuntimeAuditError(f"critical probe {classification_field} is inconsistent at imports[{index}]")
        for field in ("stderr", "stdout"):
            if (
                not isinstance(item[field], str)
                or len(item[field]) > MAX_CRITICAL_PROBE_OUTPUT
                or any(unicodedata.category(char).startswith("C") and char not in "\n\r\t" for char in item[field])
            ):
                raise RuntimeAuditError(f"critical probe imports[{index}].{field} is invalid")
    if review_modules != seen_modules:
        raise RuntimeAuditError("critical probe import_review must exactly match imports")
    has_unexecuted = any(item["status"] == "not_executed" for item in imports)
    if value["coverage"] == "partial" and not has_unexecuted:
        raise RuntimeAuditError("critical probe partial coverage has no not_executed import evidence")
    if value["coverage"] == "complete" and has_unexecuted:
        raise RuntimeAuditError("critical probe complete coverage contains not_executed imports")
    if value["status"] == "partial" and value["coverage"] != "partial":
        raise RuntimeAuditError("critical probe partial status requires partial coverage")
    if value["status"] == "pass" and value["coverage"] != "complete":
        raise RuntimeAuditError("critical probe pass status requires complete coverage")
    return value


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


def _critical_root_record(source_root: Path, path: str) -> Record | None:
    try:
        host_path = _root_path(source_root, path)
        if not host_path.exists() and not host_path.is_symlink():
            return None
        resolved = host_path.resolve(strict=True)
        if not resolved.is_relative_to(source_root.resolve(strict=True)):
            return None
        return _record(host_path, _bundle(path))
    except (OSError, RuntimeAuditError):
        return None


def _critical_entrypoint_findings(config: Mapping[str, Any], source_root: Path, selected: Mapping[str, Record], inventory: LauncherInventory) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for entry in config["entrypoints"]:
        path = str(entry["path"])
        kind = str(entry["kind"])
        status, item = _resolve_selected(path, selected)
        if status == "missing":
            item = _critical_root_record(source_root, path)
            if item is not None:
                status = "ok"
        if status == "missing":
            inv_status = _resolve_inventory(path, inventory, executable=bool(entry["required_executable"]))
            if inv_status == "provided":
                continue
            _add(findings, "critical_entrypoint_missing", "blocker", f"critical {kind} entrypoint is not provided ({inv_status})", path, {"owner": entry["owner"]} if "owner" in entry else None)
            continue
        if status != "ok" or item is None:
            _add(findings, "critical_entrypoint_unresolved", "blocker", f"critical {kind} entrypoint cannot be resolved ({status})", path)
            continue
        if entry["required_executable"] and (item.kind != "file" or not item.mode & 0o111):
            _add(findings, "critical_entrypoint_not_executable", "blocker", "critical entrypoint is not executable", path)
        if kind in ("elf", "python") and item.kind != "file":
            _add(findings, "critical_entrypoint_not_file", "blocker", "critical entrypoint is not a regular file", path)
        if kind == "script":
            data = _read(item, MAX_SHEBANG)
            try:
                parsed = _shebang(data or b"")
            except RuntimeAuditError as error:
                _add(findings, "critical_entrypoint_shebang_invalid", "blocker", str(error), path)
                continue
            expected = entry["shebang"]
            if parsed is None or parsed[0] != expected:
                _add(findings, "critical_entrypoint_shebang_mismatch", "blocker", "critical script shebang does not match contract", path, {"expected": expected, "actual": parsed[0] if parsed else None})
    return findings


def _critical_launcher_contract_report(config: Mapping[str, Any], source_root: Path, selected: Mapping[str, Record], inventory: LauncherInventory) -> list[dict[str, Any]]:
    """Describe launcher-owned paths without treating them as runtime archive inputs.

    The portability audit intentionally selects only the runtime-owned trees.
    Launcher paths therefore remain an explicit contract: an absent path is
    recorded as ``not_materialized_in_runtime`` and is expected to be supplied
    by the image launcher, rather than becoming a runtime archive blocker.
    If a launcher path happens to be present in the final rootfs, its basic
    executable/shebang contract is still checked and reported.
    """

    reports: list[dict[str, Any]] = []
    for entry in config["launcher_contract"]:
        path = str(entry["path"])
        kind = str(entry["kind"])
        status, item = _resolve_selected(path, selected)
        source = "selected-runtime"
        if status == "missing":
            item = _critical_root_record(source_root, path)
            if item is not None:
                status = "ok"
                source = "final-rootfs"
        if status == "missing":
            inventory_status = _resolve_inventory(path, inventory, executable=bool(entry["required_executable"]))
            reports.append({
                "path": path,
                "kind": kind,
                "status": "provided" if inventory_status == "provided" else "not_materialized_in_runtime",
                "provided_by": "launcher_contract",
                "inventory_status": inventory_status,
            })
            continue
        report: dict[str, Any] = {"path": path, "kind": kind, "status": "present", "provided_by": source}
        if item is None or status != "ok":
            report["status"] = "unresolved"
            report["resolution"] = status
            reports.append(report)
            continue
        if entry["required_executable"] and (item.kind != "file" or not item.mode & 0o111):
            report["status"] = "invalid"
            report["error"] = "not_executable"
        if kind == "script":
            data = _read(item, MAX_SHEBANG)
            try:
                parsed = _shebang(data or b"")
            except RuntimeAuditError as error:
                report["status"] = "invalid"
                report["error"] = str(error)
            else:
                expected = entry["shebang"]
                actual = parsed[0] if parsed else None
                report["shebang"] = {"expected": expected, "actual": actual}
                if actual != expected:
                    report["status"] = "invalid"
                    report["error"] = "shebang_mismatch"
        reports.append(report)
    return reports


def _critical_closure(config: Mapping[str, Any], selected: Mapping[str, Record], inventory: LauncherInventory, limits: Limits) -> tuple[dict[str, Any], list[dict[str, Any]], bool]:
    findings: list[dict[str, Any]] = []
    reports: list[dict[str, Any]] = []
    visited: set[str] = set()
    limited = False
    queue = [str(config["interpreter"])]
    selected_names = _selected_library_index(selected)
    while queue:
        path = queue.pop(0)
        if path in visited:
            continue
        visited.add(path)
        if len(visited) > MAX_CRITICAL_ELFS:
            limited = True
            _add(findings, "critical_elf_limit_exceeded", "blocker", "critical ELF closure limit exceeded", path)
            break
        status, item = _resolve_selected(path, selected)
        if status != "ok" or item is None:
            _add(findings, "critical_missing_elf", "blocker", f"critical ELF is not selected ({status})", path)
            continue
        if item.kind != "file" or _read(item, 4) != ELF_MAGIC:
            _add(findings, "critical_not_elf", "blocker", "critical closure node is not an ELF file", path)
            continue
        result = _readelf(item, limits)
        if result is None:
            limited = True
            _add(findings, "critical_readelf_unavailable", "blocker", "critical ELF metadata could not be inspected", path)
            continue
        try:
            metadata = _elf_metadata(result[0])
        except RuntimeAuditError as error:
            limited = True
            _add(findings, "critical_readelf_metadata_invalid", "blocker", str(error), path)
            continue
        reports.append({"path": path, **metadata})
        interpreter = metadata["interpreter"]
        if interpreter:
            status = _provision(interpreter, selected, inventory, executable=True)
            if status not in ("selected", "provided"):
                _add(findings, "critical_missing_elf_interpreter", "blocker", f"critical PT_INTERP is not provided ({status})", path, {"interpreter": interpreter})
        for name in metadata["needed"]:
            if name.startswith("/"):
                status = _provision(name, selected, inventory)
                if status not in ("selected", "provided"):
                    _add(findings, "critical_missing_elf_library", "blocker", f"critical absolute DT_NEEDED is not provided ({status})", path, {"needed": name})
                continue
            candidates = _library_candidates(name, item, metadata, selected, inventory)
            if candidates or name in inventory.library_names:
                queue.extend(candidate for candidate in candidates if candidate not in visited)
                continue
            if name in selected_names:
                _add(findings, "critical_runtime_library_present_unresolved", "blocker", "critical DT_NEEDED has selected-runtime candidate(s), but loader reachability is unproven", path, {"needed": name, "candidate_count": len(selected_names[name]), "candidates": list(selected_names[name][:16])})
            else:
                _add(findings, "critical_missing_elf_library", "blocker", "critical DT_NEEDED is absent from selected runtime and launcher inventory", path, {"needed": name})
    return {"elfs": sorted(reports, key=lambda value: value["path"]), "visited": sorted(visited)}, findings, limited


def _critical_report(config: Mapping[str, Any], source_root: Path, selected: Mapping[str, Record], inventory: LauncherInventory, limits: Limits, probe: Mapping[str, Any] | None, probe_profile: str | None = None) -> dict[str, Any]:
    findings = _critical_entrypoint_findings(config, source_root, selected, inventory)
    closure, closure_findings, closure_limited = _critical_closure(config, selected, inventory, limits)
    findings.extend(closure_findings)
    imports = [] if probe is None else list(probe["imports"])
    expected_probe_digest = hashlib.sha256(json.dumps(config, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    expected_probe_profile = probe_profile or str(config["default_probe_profile"])
    if expected_probe_profile not in CRITICAL_IMPORT_PROFILES:
        raise RuntimeAuditError("critical portability probe profile is invalid")
    if probe is None:
        _add(findings, "critical_probe_missing", "blocker", "critical import probe report was not supplied")
    elif probe["probe_profile"] != expected_probe_profile:
        _add(
            findings,
            "critical_probe_profile_mismatch",
            "blocker",
            "critical import probe profile does not match the configured portability profile",
            str(probe["probe_profile"]),
            {"expected": expected_probe_profile},
        )
    elif probe["status"] in {"blocker", "limited"}:
        _add(findings, "critical_probe_failed", "blocker", "critical import probe did not pass")
    elif probe["status"] == "partial" and expected_probe_profile != "cpu":
        _add(findings, "critical_probe_partial_not_allowed", "blocker", "GPU-required critical probe must execute every configured import")
    elif probe["config_sha256"] != expected_probe_digest:
        _add(findings, "critical_probe_config_mismatch", "blocker", "critical import probe was generated from a different config")
    else:
        expected_imports = {
            str(item["module"]): {
                "required": bool(item["required"]),
                "profile": str(item["profile"]),
            }
            for item in config["imports"]
        }
        runnable_profiles = CRITICAL_PROFILE_IMPORTS[expected_probe_profile]
        actual_imports: dict[str, Mapping[str, Any]] = {}
        for item in imports:
            module = str(item["module"])
            if module in actual_imports:
                _add(findings, "critical_probe_duplicate_import", "blocker", "critical import probe contains a duplicate module", module)
            actual_imports[module] = item
            expected = expected_imports.get(module)
            if expected is None or bool(item["required"]) != expected["required"] or item["profile"] != expected["profile"]:
                _add(findings, "critical_probe_import_mismatch", "blocker", "critical import probe module metadata does not match config", module)
                continue
            if expected["profile"] not in runnable_profiles:
                if item["status"] != "not_executed" or item.get("reason_code") != CRITICAL_SKIP_REASON:
                    _add(
                        findings,
                        "critical_probe_profile_status_invalid",
                        "blocker",
                        "GPU-required import must be recorded as not_executed with environment_unavailable in a CPU probe",
                        module,
                    )
            elif expected["required"] and item["status"] != "pass":
                _add(findings, "critical_probe_required_import_failed", "blocker", "required critical import did not pass", module)
            elif item["status"] == "pass":
                external_mappings = sorted({
                    path
                    for category in ("launcher_or_system", "other")
                    for path in item["cumulative_shared_object_classification"][category]
                })
                for mapped_path in external_mappings:
                    if _resolve_inventory(mapped_path, inventory) == "provided" or posixpath.basename(mapped_path) in inventory.library_names:
                        continue
                    _add(
                        findings,
                        "critical_probe_unprovided_shared_object",
                        "blocker",
                        "executed critical import mapped a shared object outside the runtime archive that is absent from launcher inventory",
                        module,
                        {"mapped_path": mapped_path},
                    )
        missing_imports = sorted(set(expected_imports) - set(actual_imports))
        for module in missing_imports:
            _add(findings, "critical_probe_import_missing", "blocker", "critical import is absent from probe report", module)
        if probe["main_script_compile"]["status"] != "pass":
            _add(findings, "critical_main_script_compile_failed", "blocker", "critical main.py compile-only validation did not pass", str(config["main_script"]))
    findings, truncated = _sorted_findings(findings, limits.max_findings)
    if truncated:
        findings.append({"code": "critical_finding_limit_exceeded", "severity": "blocker", "path": "", "detail": "critical finding limit exceeded; report is incomplete"})
    counts = {severity: sum(1 for value in findings if value["severity"] == severity) for severity in SEVERITIES}
    status = "blocker" if counts["blocker"] else "limited" if closure_limited or truncated else "pass"
    if status == "pass" and probe is not None and probe.get("coverage") == "partial":
        status = "partial"
    return {
        "status": status,
        "profile": config["profile"],
        "probe_profile": expected_probe_profile,
        "config": config,
        "probe": probe,
        "entrypoints": config["entrypoints"],
        "launcher_contract": config["launcher_contract"],
        "launcher_contract_report": _critical_launcher_contract_report(config, source_root, selected, inventory),
        "closure": closure,
        "imports": imports,
        "findings": findings,
        "finding_counts": counts,
    }


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


def _selected_library_index(selected: Mapping[str, Record]) -> dict[str, tuple[str, ...]]:
    """Index selected files by basename without claiming loader reachability.

    A basename match is useful evidence that a dependency is contained in the
    runtime tree, but it is not enough to call the dependency resolved: the
    dynamic loader may not search that directory.  Callers use this index only
    to distinguish a runtime/configuration problem from a true launcher
    requirement.
    """

    index: dict[str, list[str]] = {}
    for path, item in selected.items():
        if item.kind not in ("file", "symlink"):
            continue
        status, resolved = _resolve_selected(path, selected)
        if status != "ok" or resolved is None or resolved.kind != "file":
            continue
        name = posixpath.basename(path)
        if not name:
            continue
        index.setdefault(name, []).append(path)
    return {name: tuple(sorted(paths)) for name, paths in index.items()}


def _library_candidates(name: str, item: Record, metadata: Mapping[str, Any], selected: Mapping[str, Record], inventory: LauncherInventory) -> list[str]:
    paths: list[str] = []
    for raw in [*metadata["rpath"], *metadata["runpath"]]:
        for component in raw.split(":"):
            directory = _origin_path(component, item)
            if directory:
                candidate = posixpath.join(directory, name)
                status, resolved = _resolve_selected(candidate, selected)
                if status == "ok" and resolved is not None and resolved.kind == "file" and candidate not in paths:
                    paths.append(candidate)
    return paths


def check_elfs(records: Sequence[Record], selected: Mapping[str, Record], inventory: LauncherInventory, findings: list[dict[str, Any]], required_paths: set[str], required_libraries: set[str], required_library_paths: set[str], limits: Limits) -> tuple[int, list[dict[str, Any]], bool]:
    count = 0
    reports: list[dict[str, Any]] = []
    limited = False
    selected_library_index = _selected_library_index(selected)
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
            elif name in selected_library_index:
                # A same-named file elsewhere in the selected tree proves
                # that the runtime contains the dependency, but not that the
                # loader can reach it.  Keep it out of launcher requirements;
                # the image/runtime loader configuration still needs review.
                candidates = selected_library_index[name]
                _add(
                    findings,
                    "runtime_library_present_unresolved",
                    "blocker",
                    "DT_NEEDED has selected-runtime candidate(s), but static audit cannot prove loader reachability",
                    item.path,
                    {"needed": name, "candidates": list(candidates[:16]), "candidate_count": len(candidates)},
                )
            else:
                required_libraries.add(name)
                _add(findings, "missing_elf_library", "blocker", "DT_NEEDED is absent from selected runtime and launcher inventory", item.path, {"needed": name})
        for kind in ("rpath", "runpath"):
            for raw in metadata[kind]:
                for component in raw.split(":"):
                    if not component:
                        # An empty loader path component means the process
                        # current working directory. That implicit location
                        # is runtime/launcher configuration, not a path the
                        # launcher inventory can safely provide.
                        _add(
                            findings,
                            "implicit_cwd_elf_search_path",
                            "blocker",
                            f"{kind.upper()} contains an empty component that depends on the process current working directory",
                            item.path,
                            {kind: raw},
                        )
                        continue
                    directory = _origin_path(component, item)
                    if directory is None:
                        _add(findings, "unresolved_elf_search_path", "blocker", f"{kind.upper()} component is not absolute or $ORIGIN-relative", item.path, {kind: component})
                    elif any(_prefix(directory, prefix) for prefix in MUTABLE_PREFIXES):
                        _add(findings, "elf_search_path_mutable", "blocker", f"{kind.upper()} points into mutable or excluded state", item.path, {kind: directory})
                    elif directory not in selected and not any(_prefix(directory, value) for value in inventory.library_paths):
                        # An RPATH/RUNPATH directory need not exist when the
                        # dependency is absent, and it is not by itself a
                        # launcher contract.  Dependency reachability is
                        # decided from DT_NEEDED above; keep this as a warning
                        # without polluting launcher requirements.
                        _add(findings, "missing_elf_search_path", "warning", f"{kind.upper()} directory is not present in selected runtime or launcher inventory", item.path, {kind: directory})
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


def audit_runtime(*, source_root: Path, targets: Sequence[str] = DEFAULT_TARGETS, app_files: Sequence[str] = (), exclusions: Sequence[str] = DEFAULT_EXCLUDES, launcher_inventory: LauncherInventory | None = None, limits: Limits | None = None, critical_config: Mapping[str, Any] | None = None, critical_probe: Mapping[str, Any] | None = None, critical_profile: str | None = None) -> dict[str, Any]:
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
    report = {
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
        "provided_launcher_inventory": {"system_paths": list(inventory.system_paths), "libraries": list(inventory.libraries), "library_paths": list(inventory.library_paths), "executable_paths": list(inventory.executable_paths), "symlinks": [{"path": source, "target": target} for source, target in inventory.symlinks]},
    }
    if critical_config is not None:
        report["critical"] = _critical_report(critical_config, source_root, selected, inventory, active_limits, critical_probe, critical_profile)
        critical_status = report["critical"]["status"]
        report["gate"] = {
            "status": "pass" if critical_status in {"pass", "partial"} else critical_status,
            "source": "critical",
            "profile": report["critical"]["profile"],
            "probe_profile": report["critical"]["probe_profile"],
            "evidence_status": critical_status,
        }
    else:
        report["gate"] = {
            "status": status,
            "source": "whole_rootfs",
            "profile": None,
            "probe_profile": None,
            "evidence_status": status,
        }
    return report


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
    parser.add_argument("--critical-config", type=Path)
    parser.add_argument("--critical-probe", type=Path)
    parser.add_argument("--critical-profile", choices=tuple(CRITICAL_PROFILES))
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
        critical_config = load_critical_config(args.critical_config) if args.critical_config else None
        critical_probe = load_critical_probe(args.critical_probe) if args.critical_probe else None
        if critical_probe is not None and critical_config is None:
            raise RuntimeAuditError("--critical-probe requires --critical-config")
        report = audit_runtime(source_root=args.source_root, targets=policy["targets"], app_files=policy["include_app"], exclusions=policy["excludes"], launcher_inventory=inventory, limits=Limits(args.max_entries, args.max_shebang_bytes, args.max_findings), critical_config=critical_config, critical_probe=critical_probe, critical_profile=args.critical_profile)
        rendered = render_report(report)
        if args.output:
            args.output.write_text(rendered, encoding="utf-8")
        else:
            sys.stdout.write(rendered)
        return 1 if report["gate"]["status"] in ("blocker", "limited") else 0
    except (RuntimeAuditError, OSError) as error:
        print(f"runtime portability audit failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
