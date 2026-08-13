#!/usr/bin/env python3
"""Run the repository-owned critical Python import probe.

The probe is intended to run inside the exact final base image with a
read-only rootfs, no network, and only ``/tmp`` writable.  It executes only
the allowlisted import modules from the signed-in-repository config; it never
executes an ELF discovered by the portability scanner.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib
import io
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any


AUDIT_SCRIPT = Path(__file__).resolve().with_name("runtime_portability_audit.py")
if str(AUDIT_SCRIPT.parent) not in sys.path:
    sys.path.insert(0, str(AUDIT_SCRIPT.parent))

from runtime_portability_audit import (  # noqa: E402
    CRITICAL_IMPORT_ALLOWLIST,
    CRITICAL_IMPORT_PROFILES,
    CRITICAL_PROFILE_POLICIES,
    CRITICAL_PROFILE_IMPORTS,
    CRITICAL_POLICY,
    CRITICAL_PROBE_VERSION,
    MAX_CRITICAL_SOURCE_BYTES,
    RuntimeAuditError,
    load_critical_config,
)


IMPORT_TIMEOUT_SECONDS = 45
MAX_OUTPUT_BYTES = 256 * 1024
POLICY = dict(CRITICAL_POLICY)


class ImportTimeout(Exception):
    """Raised when one controlled import exceeds its bounded time."""


def _timeout_handler(_signum: int, _frame: object) -> None:
    raise ImportTimeout("critical import timed out")


def _truncate(value: str) -> str:
    encoded = value.encode("utf-8", "replace")
    if len(encoded) <= MAX_OUTPUT_BYTES:
        return value
    return encoded[:MAX_OUTPUT_BYTES].decode("utf-8", "ignore")


def _mapped_files() -> list[str]:
    values: set[str] = set()
    try:
        lines = Path("/proc/self/maps").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    for line in lines:
        fields = line.split()
        if len(fields) < 6:
            continue
        path = fields[-1]
        if not path.startswith("/"):
            continue
        if path.endswith(" (deleted)"):
            path = path[:-10]
        values.add(path)
    return sorted(values)


def _shared_objects(paths: list[str]) -> list[str]:
    result: list[str] = []
    for path in paths:
        name = os.path.basename(path)
        if ".so" in name or name.startswith("ld-") or name.startswith("python"):
            result.append(path)
    return sorted(set(result))


def _classify_shared_objects(paths: list[str]) -> dict[str, list[str]]:
    runtime: list[str] = []
    launcher_or_system: list[str] = []
    other: list[str] = []
    for path in paths:
        if path == "/opt/conda" or path.startswith("/opt/conda/") or path == "/app/comfyui" or path.startswith("/app/comfyui/"):
            runtime.append(path)
        elif path.startswith(("/bin/", "/lib/", "/lib64/", "/sbin/", "/usr/bin/", "/usr/lib/", "/usr/lib64/", "/usr/local/bin/", "/usr/local/lib/")):
            launcher_or_system.append(path)
        else:
            other.append(path)
    return {
        "runtime": sorted(set(runtime)),
        "launcher_or_system": sorted(set(launcher_or_system)),
        "other": sorted(set(other)),
    }


def _import_one(module: str, required: bool, profile: str = "cpu") -> dict[str, Any]:
    if module not in CRITICAL_IMPORT_ALLOWLIST:
        raise RuntimeAuditError(f"module is not allowlisted: {module}")
    stdout = io.StringIO()
    stderr = io.StringIO()
    started = time.monotonic()
    before_mapped = _mapped_files()
    status = "pass"
    error_text = ""
    old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
    signal.setitimer(signal.ITIMER_REAL, IMPORT_TIMEOUT_SECONDS)
    try:
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            importlib.import_module(module)
    except ImportTimeout as error:
        status = "timeout"
        error_text = str(error)
    except BaseException as error:  # noqa: BLE001 - probe must report import failures
        status = "failed"
        error_text = f"{type(error).__name__}: {error}"
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)
    if error_text:
        stderr.write(error_text)
    mapped = _mapped_files()
    shared_objects = _shared_objects(mapped)
    new_mapped = sorted(set(mapped) - set(before_mapped))
    before_shared_objects = _shared_objects(before_mapped)
    new_shared_objects = sorted(set(shared_objects) - set(before_shared_objects))
    return {
        "module": module,
        "required": required,
        "profile": profile,
        "status": status,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "before_shared_objects": before_shared_objects,
        "before_shared_object_classification": _classify_shared_objects(before_shared_objects),
        "before_mapped_files": before_mapped,
        "cumulative_shared_objects": shared_objects,
        "cumulative_shared_object_classification": _classify_shared_objects(shared_objects),
        "cumulative_mapped_files": mapped,
        "new_shared_objects": new_shared_objects,
        "new_shared_object_classification": _classify_shared_objects(new_shared_objects),
        "new_mapped_files": new_mapped,
        "stderr": _truncate(stderr.getvalue()),
        "stdout": _truncate(stdout.getvalue()),
    }


def _not_executed(module: str, required: bool, profile: str) -> dict[str, Any]:
    """Record a deliberately skipped profile check without claiming success."""

    return {
        "module": module,
        "required": required,
        "profile": profile,
        "status": "not_executed",
        "duration_ms": 0,
        "reason_code": "environment_unavailable",
        "before_shared_objects": [],
        "before_shared_object_classification": {"runtime": [], "launcher_or_system": [], "other": []},
        "before_mapped_files": [],
        "cumulative_shared_objects": [],
        "cumulative_shared_object_classification": {"runtime": [], "launcher_or_system": [], "other": []},
        "cumulative_mapped_files": [],
        "new_shared_objects": [],
        "new_shared_object_classification": {"runtime": [], "launcher_or_system": [], "other": []},
        "new_mapped_files": [],
        "stderr": "",
        "stdout": "",
    }


def _compile_main_script(config: dict[str, Any]) -> dict[str, Any]:
    """Compile main.py without executing it or importing custom nodes."""

    path = Path(config["main_script"])
    try:
        source = path.read_bytes()
    except OSError as error:
        return {"path": str(path), "status": "failed", "source_bytes": 0, "error": f"{type(error).__name__}: {error}"}
    if len(source) > MAX_CRITICAL_SOURCE_BYTES:
        return {"path": str(path), "status": "failed", "source_bytes": len(source), "error": "main script exceeds probe source-size limit"}
    try:
        compile(source, str(path), "exec")
    except (SyntaxError, ValueError, TypeError) as error:
        return {"path": str(path), "status": "failed", "source_bytes": len(source), "error": f"{type(error).__name__}: {error}"}
    return {"path": str(path), "status": "pass", "source_bytes": len(source)}


def run_probe(config: dict[str, Any], probe_profile: str | None = None) -> dict[str, Any]:
    selected_profile = probe_profile or config["default_probe_profile"]
    if selected_profile not in CRITICAL_IMPORT_PROFILES:
        raise RuntimeAuditError(f"unsupported critical probe profile: {selected_profile}")
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    os.environ["PYTHONNOUSERSITE"] = "1"
    if selected_profile == "cpu":
        os.environ["CUDA_VISIBLE_DEVICES"] = ""
    # A GPU smoke caller owns device selection. Do not create or overwrite
    # CUDA_VISIBLE_DEVICES for that profile: an inherited restriction remains
    # evidence, while an absent variable leaves the provider-visible devices
    # available to the import checks.
    # Do not inherit a potentially read-only image home or an image-provided
    # user site/PYTHONPATH.  The runtime itself is limited to /tmp for
    # temporary writes; /audit is the dedicated evidence sink mounted by CI.
    # The probe must import the exact interpreter environment under audit.
    os.environ["HOME"] = "/tmp"
    os.environ["XDG_CACHE_HOME"] = "/tmp/xdg-cache"
    os.environ["XDG_CONFIG_HOME"] = "/tmp/xdg-config"
    os.environ["XDG_DATA_HOME"] = "/tmp/xdg-data"
    os.environ["TMPDIR"] = "/tmp"
    os.environ.pop("PYTHONPATH", None)
    Path("/tmp/xdg-cache").mkdir(parents=True, exist_ok=True)
    Path("/tmp/xdg-config").mkdir(parents=True, exist_ok=True)
    Path("/tmp/xdg-data").mkdir(parents=True, exist_ok=True)
    os.chdir(config["working_directory"])
    if config["working_directory"] not in sys.path:
        sys.path.insert(0, config["working_directory"])

    main_script_compile = _compile_main_script(config)
    imports = [item for item in config["imports"] if item["module"] in CRITICAL_IMPORT_ALLOWLIST]
    runnable_profiles = CRITICAL_PROFILE_IMPORTS[selected_profile]
    results: list[dict[str, Any]] = []
    for item in imports:
        if item["profile"] in runnable_profiles:
            result = _import_one(item["module"], item["required"], item["profile"])
        else:
            result = _not_executed(item["module"], item["required"], item["profile"])
        results.append(result)
    required_failed = main_script_compile["status"] != "pass" or any(
        item["required"] and item["status"] != "pass"
        for item in results
        if item["profile"] in runnable_profiles
    )
    has_unexecuted = any(item["status"] == "not_executed" for item in results)
    return {
        "schema_version": CRITICAL_PROBE_VERSION,
        "profile": config["profile"],
        "probe_profile": selected_profile,
        "config_sha256": hashlib.sha256(json.dumps(config, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest(),
        "status": "blocker" if required_failed else "partial" if has_unexecuted else "pass",
        "coverage": "partial" if has_unexecuted else "complete",
        "policy": CRITICAL_PROFILE_POLICIES[selected_profile],
        "main_script_compile": main_script_compile,
        "import_review": config["import_review"],
        "imports": results,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--profile", choices=("cpu", "gpu_required"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    report: dict[str, Any]
    try:
        config = load_critical_config(args.config)
        report = run_probe(config, args.profile)
        exit_code = 1 if report["status"] == "blocker" else 0
    except (RuntimeAuditError, OSError) as error:
        report = {
            "schema_version": CRITICAL_PROBE_VERSION,
            "profile": "base",
            "probe_profile": args.profile or "cpu",
            "config_sha256": "0" * 64,
            "status": "blocker",
            "coverage": "incomplete",
            "policy": CRITICAL_PROFILE_POLICIES.get(args.profile or "cpu", POLICY),
            "main_script_compile": {"path": "/app/comfyui/main.py", "status": "failed", "source_bytes": 0, "error": str(error)},
            "import_review": [],
            "imports": [],
            "error": str(error),
        }
        exit_code = 2
    args.output.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
