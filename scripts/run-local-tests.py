#!/usr/bin/env python3
"""Local test runner for ComfyUI custom node test workflows.

Provides two modes:
  1. Structure-only validation (no ComfyUI needed)
  2. Execution against a running ComfyUI instance

This is the canonical local equivalent of what runs in CI, ensuring authors
can validate their test workflows before submitting.

Usage:
    # Validate structure only
    python scripts/run-local-tests.py --pack comfyui-example --validate-only

    # Execute against local ComfyUI
    python scripts/run-local-tests.py --pack comfyui-example --url http://localhost:8188

    # Execute against ephemeral env with API key
    python scripts/run-local-tests.py --pack comfyui-example --url https://pr-123.testenvs.comfy.org --api-key $API_KEY

    # Run all packs
    python scripts/run-local-tests.py --all --validate-only

    # JSON output for CI
    python scripts/run-local-tests.py --pack comfyui-example --url http://localhost:8188 --json
"""

import argparse
import json
import os
import sys
import time

# ---------------------------------------------------------------------------
# Import shared validation logic from sibling scripts.
# ---------------------------------------------------------------------------
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from validate_test_workflows import validate_workflow  # noqa: E402

# Optional dependencies -- degrade gracefully when missing.
try:
    import requests

    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

try:
    import yaml  # noqa: F401 — only needed for --all discovery

    HAS_YAML = True
except ImportError:
    HAS_YAML = False

try:
    from PIL import Image
    import math

    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False

try:
    import imagehash

    HAS_IMAGEHASH = True
except ImportError:
    HAS_IMAGEHASH = False


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DEFAULT_TESTS_DIR_NAME = os.path.join("tests", "node-tests")
DEFAULT_TEST_WORKFLOWS_DIR_NAME = "test_workflows"
DEFAULT_TIMEOUT = 120
POLL_INTERVAL = 2

# Output node class_types that indicate a workflow has at least one output.
# This list covers common ComfyUI output nodes; custom nodes may add more.
OUTPUT_CLASS_TYPES = {
    "SaveImage",
    "PreviewImage",
    "SaveAnimatedWEBP",
    "SaveAnimatedPNG",
    "SaveLatent",
    "SaveAudio",
    "TestExpectation",
    "TestOutput",
    "AssertExecuted",
    "TestDefinition",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _project_root():
    """Return the project root (one level up from scripts/)."""
    return os.path.dirname(SCRIPTS_DIR)


def _resolve_tests_dir(args_tests_dir):
    """Resolve the tests directory, trying multiple conventions."""
    if args_tests_dir:
        return args_tests_dir

    root = _project_root()
    # Convention 1: test_workflows/<pack>/api/
    tw = os.path.join(root, DEFAULT_TEST_WORKFLOWS_DIR_NAME)
    if os.path.isdir(tw):
        return tw

    # Convention 2: tests/node-tests/<pack>/
    nt = os.path.join(root, DEFAULT_TESTS_DIR_NAME)
    if os.path.isdir(nt):
        return nt

    return nt  # default even if missing


def _discover_workflows(tests_dir, pack_name):
    """Find test workflow JSON files for a pack.

    Looks in:
      - <tests_dir>/<pack>/api/*.json   (test_workflows convention)
      - <tests_dir>/<pack>/e2e/*.json   (e2e convention)
      - <tests_dir>/<pack>/*.json        (flat convention, e.g. tests/node-tests)

    Returns dict: {"api": [paths], "e2e": [paths]}
    """
    result = {"api": [], "e2e": []}
    pack_dir = os.path.join(tests_dir, pack_name)

    if not os.path.isdir(pack_dir):
        return result

    api_dir = os.path.join(pack_dir, "api")
    e2e_dir = os.path.join(pack_dir, "e2e")

    # If api/ subdirectory exists, use the structured layout
    if os.path.isdir(api_dir):
        for f in sorted(os.listdir(api_dir)):
            if f.endswith(".json") and not f.endswith(".config.json"):
                result["api"].append(os.path.join(api_dir, f))
    elif os.path.isdir(e2e_dir):
        # Only e2e dir
        pass
    else:
        # Flat layout: all .json files directly in pack dir
        for f in sorted(os.listdir(pack_dir)):
            if f.endswith(".json") and not f.endswith(".config.json"):
                result["api"].append(os.path.join(pack_dir, f))

    if os.path.isdir(e2e_dir):
        for f in sorted(os.listdir(e2e_dir)):
            if f.endswith(".json") and not f.endswith(".config.json"):
                result["e2e"].append(os.path.join(e2e_dir, f))

    return result


def _discover_all_packs(tests_dir):
    """List all pack directories under the tests dir."""
    if not os.path.isdir(tests_dir):
        return []
    return sorted(
        d
        for d in os.listdir(tests_dir)
        if os.path.isdir(os.path.join(tests_dir, d))
    )


def _load_workflow(filepath):
    """Load and return parsed JSON from a workflow file."""
    with open(filepath, "r", encoding="utf-8") as f:
        return json.load(f)


def _count_nodes(data):
    """Count nodes in a workflow dict."""
    if not isinstance(data, dict):
        return 0
    return sum(1 for v in data.values() if isinstance(v, dict) and "class_type" in v)


def _count_output_nodes(data):
    """Count output nodes in a workflow dict."""
    if not isinstance(data, dict):
        return 0
    count = 0
    for v in data.values():
        if isinstance(v, dict):
            ct = v.get("class_type", "")
            # Check known output types or names containing common output patterns
            if (ct in OUTPUT_CLASS_TYPES or "Save" in ct or "Preview" in ct
                    or "Output" in ct or "Assert" in ct):
                count += 1
    return count


def _collect_node_refs(data):
    """Collect all [node_id, index] references and defined node IDs."""
    defined_ids = set()
    refs = []

    if not isinstance(data, dict):
        return defined_ids, refs

    for node_id, node_config in data.items():
        if not isinstance(node_config, dict):
            continue
        defined_ids.add(str(node_id))

        inputs = node_config.get("inputs", {})
        if not isinstance(inputs, dict):
            continue

        for input_name, input_val in inputs.items():
            if isinstance(input_val, list) and len(input_val) == 2:
                ref_id, ref_idx = input_val
                if isinstance(ref_id, (str, int)) and isinstance(ref_idx, int):
                    refs.append((str(ref_id), ref_idx, node_id, input_name))

    return defined_ids, refs


def _check_metadata(data):
    """Check if test metadata is present at extra_data.extra_pnginfo._test_metadata."""
    extra_data = data.get("extra_data", {})
    if not isinstance(extra_data, dict):
        return False
    extra_pnginfo = extra_data.get("extra_pnginfo", {})
    if not isinstance(extra_pnginfo, dict):
        return False
    return "_test_metadata" in extra_pnginfo


# ---------------------------------------------------------------------------
# Structure validation (Mode 1)
# ---------------------------------------------------------------------------

class ValidationResult:
    """Result of validating a single workflow file."""

    def __init__(self, filepath, errors, warnings, node_count=0, output_count=0):
        self.filepath = filepath
        self.filename = os.path.basename(filepath)
        self.errors = errors
        self.warnings = warnings
        self.node_count = node_count
        self.output_count = output_count
        self.passed = len(errors) == 0


def validate_workflow_extended(filepath):
    """Extended validation beyond what validate_test_workflows.py checks.

    Reuses the base validation and adds:
      - Output node presence check
      - Dangling reference detection
      - Metadata presence check
    """
    errors = []
    warnings = []
    node_count = 0
    output_count = 0

    # Base validation from shared module
    base_errors = validate_workflow(filepath)
    if base_errors:
        # If base validation fails (invalid JSON, not a dict, etc.), stop early
        return ValidationResult(filepath, base_errors, warnings)

    try:
        data = _load_workflow(filepath)
    except (json.JSONDecodeError, OSError) as e:
        errors.append(f"Failed to load: {e}")
        return ValidationResult(filepath, errors, warnings)

    # Check it's a dict with a prompt key, or is the prompt itself
    # The PRD says "Has `prompt` key with node definitions" but existing
    # test workflows use the flat format (dict of node_id -> config).
    # Support both: if top-level has "prompt", use that; otherwise treat
    # the whole dict as the prompt.
    prompt = data
    if "prompt" in data and isinstance(data["prompt"], dict):
        prompt = data["prompt"]
    elif all(
        isinstance(v, dict) and "class_type" in v
        for v in data.values()
        if isinstance(v, dict)
    ):
        # Flat format (the existing convention)
        prompt = data
    # If neither pattern matches, base validation already caught it.

    node_count = _count_nodes(prompt)
    output_count = _count_output_nodes(prompt)

    # Check for at least one output node
    if output_count == 0:
        errors.append("missing output node")

    # Check for dangling references
    defined_ids, refs = _collect_node_refs(prompt)
    for ref_id, ref_idx, src_node, src_input in refs:
        if ref_id not in defined_ids:
            errors.append(
                f"dangling reference: node '{src_node}' input '{src_input}' "
                f"references undefined node '{ref_id}'"
            )

    # Check metadata (warning, not error — existing workflows may lack it)
    if not _check_metadata(data):
        warnings.append("missing _test_metadata at extra_data.extra_pnginfo._test_metadata")

    return ValidationResult(filepath, errors, warnings, node_count, output_count)


# ---------------------------------------------------------------------------
# Execution (Mode 2)
# ---------------------------------------------------------------------------

def _make_session(api_key=None):
    """Create a requests session with optional API key auth."""
    if not HAS_REQUESTS:
        raise RuntimeError(
            "The 'requests' library is required for execution mode. "
            "Install it with: pip install requests"
        )
    session = requests.Session()
    if api_key:
        session.headers["Authorization"] = f"Bearer {api_key}"
    return session


def _check_comfyui_reachable(session, url):
    """Check that a ComfyUI instance is reachable."""
    try:
        resp = session.get(f"{url.rstrip('/')}/api/object_info", timeout=10)
        resp.raise_for_status()
        return True, None
    except requests.ConnectionError:
        return False, (
            f"Cannot connect to ComfyUI at {url}. "
            "Make sure ComfyUI is running and the URL is correct."
        )
    except requests.Timeout:
        return False, f"Connection to {url} timed out."
    except requests.RequestException as e:
        return False, f"Error connecting to {url}: {e}"


def _upload_input_files(session, url, config):
    """Upload input files specified in a config dict."""
    input_files = config.get("input_files", [])
    if not input_files:
        return True, None

    upload_url = f"{url.rstrip('/')}/api/upload/image"
    for file_entry in input_files:
        fpath = file_entry if isinstance(file_entry, str) else file_entry.get("path", "")
        subfolder = "" if isinstance(file_entry, str) else file_entry.get("subfolder", "")

        if not os.path.isfile(fpath):
            return False, f"Input file not found: {fpath}"

        with open(fpath, "rb") as f:
            files = {"image": (os.path.basename(fpath), f)}
            form_data = {}
            if subfolder:
                form_data["subfolder"] = subfolder
            try:
                resp = session.post(upload_url, files=files, data=form_data, timeout=30)
                resp.raise_for_status()
            except requests.RequestException as e:
                return False, f"Failed to upload {fpath}: {e}"

    return True, None


class ExecutionResult:
    """Result of executing a single workflow."""

    def __init__(self, filepath, success, message, elapsed=0.0, skipped=False):
        self.filepath = filepath
        self.filename = os.path.basename(filepath)
        self.success = success
        self.message = message
        self.elapsed = elapsed
        self.skipped = skipped


def execute_workflow(session, url, filepath, timeout=DEFAULT_TIMEOUT):
    """Execute a workflow against a ComfyUI instance.

    Returns an ExecutionResult.
    """
    try:
        data = _load_workflow(filepath)
    except (json.JSONDecodeError, OSError) as e:
        return ExecutionResult(filepath, False, f"failed to load: {e}")

    # Determine prompt payload — support both {prompt: {...}} and flat format
    if "prompt" in data and isinstance(data["prompt"], dict):
        payload = data
    else:
        payload = {"prompt": data}

    prompt_url = f"{url.rstrip('/')}/api/prompt"
    start = time.time()

    try:
        resp = session.post(prompt_url, json=payload, timeout=30)
        resp.raise_for_status()
        resp_data = resp.json()
    except requests.ConnectionError:
        return ExecutionResult(filepath, False, "ComfyUI not reachable")
    except requests.RequestException as e:
        return ExecutionResult(filepath, False, f"submit error: {e}")

    prompt_id = resp_data.get("prompt_id")
    if not prompt_id:
        error_msg = resp_data.get("error", resp_data.get("node_errors", "unknown"))
        return ExecutionResult(filepath, False, f"rejected: {error_msg}")

    # Poll for completion
    history_url = f"{url.rstrip('/')}/api/history/{prompt_id}"

    while True:
        elapsed = time.time() - start
        if elapsed > timeout:
            return ExecutionResult(filepath, False, f"timed out after {timeout}s", elapsed)

        time.sleep(POLL_INTERVAL)

        try:
            resp = session.get(history_url, timeout=10)
            resp.raise_for_status()
            history = resp.json()
        except requests.RequestException:
            continue

        if prompt_id not in history:
            continue

        entry = history[prompt_id]
        status = entry.get("status", {})
        completed = status.get("completed", False)
        status_str = status.get("status_str", "unknown")

        if completed:
            elapsed = time.time() - start
            # Check for node errors
            outputs = entry.get("outputs", {})
            for node_id, node_output in outputs.items():
                if "errors" in node_output:
                    err_detail = node_output["errors"]
                    if isinstance(err_detail, list) and err_detail:
                        err_msg = err_detail[0].get("message", str(err_detail))
                    else:
                        err_msg = str(err_detail)
                    return ExecutionResult(
                        filepath, False, f"execution error: {err_msg}", elapsed
                    )
            return ExecutionResult(filepath, True, f"{elapsed:.1f}s", elapsed)

        if status_str == "error":
            elapsed = time.time() - start
            messages = status.get("messages", [])
            err_msg = "; ".join(str(m) for m in messages) if messages else "unknown error"
            return ExecutionResult(
                filepath, False, f"execution error: {err_msg}", elapsed
            )


# ---------------------------------------------------------------------------
# E2E comparison
# ---------------------------------------------------------------------------

class ComparisonResult:
    """Result of an E2E comparison for a single workflow."""

    def __init__(self, filepath, success, message):
        self.filepath = filepath
        self.filename = os.path.basename(filepath)
        self.success = success
        self.message = message


def _compute_rms(img_a, img_b):
    """Compute RMS difference between two PIL Images."""
    if not HAS_PILLOW:
        return None
    # Convert to same mode and size
    img_a = img_a.convert("RGB")
    img_b = img_b.convert("RGB")
    if img_a.size != img_b.size:
        img_b = img_b.resize(img_a.size)

    pixels_a = list(img_a.getdata())
    pixels_b = list(img_b.getdata())

    sum_sq = 0.0
    n = len(pixels_a) * 3  # 3 channels
    for pa, pb in zip(pixels_a, pixels_b):
        for ca, cb in zip(pa, pb):
            sum_sq += (ca - cb) ** 2

    return math.sqrt(sum_sq / n) if n > 0 else 0.0


def _compute_phash(img):
    """Compute perceptual hash of a PIL Image."""
    if not HAS_IMAGEHASH or not HAS_PILLOW:
        return None
    return imagehash.phash(img)


def run_e2e_comparison(session, url, filepath, config_path):
    """Run E2E comparison for a single workflow based on its config."""
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        return ComparisonResult(filepath, False, f"config load error: {e}")

    mode = config.get("comparison_mode", "hamming")
    threshold = config.get("threshold", 0)

    # We need the output from the execution. If URL is provided, we expect
    # the output to have been saved. The config should specify output_node_id
    # or we find the first output.
    output_url_path = config.get("output_path")
    if not output_url_path:
        return ComparisonResult(filepath, False, "config missing 'output_path'")

    # Fetch the output image from ComfyUI
    full_output_url = f"{url.rstrip('/')}/{output_url_path.lstrip('/')}"
    try:
        resp = session.get(full_output_url, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        return ComparisonResult(filepath, False, f"failed to fetch output: {e}")

    if mode == "hamming":
        if not HAS_IMAGEHASH or not HAS_PILLOW:
            return ComparisonResult(
                filepath, False,
                "hamming comparison requires 'Pillow' and 'imagehash' packages"
            )
        expected_hash_str = config.get("expected_hash")
        if not expected_hash_str:
            return ComparisonResult(filepath, False, "config missing 'expected_hash'")

        from io import BytesIO

        img = Image.open(BytesIO(resp.content))
        actual_hash = _compute_phash(img)
        expected_hash = imagehash.hex_to_hash(expected_hash_str)
        distance = actual_hash - expected_hash

        if distance <= threshold:
            return ComparisonResult(
                filepath, True, f"hamming distance: {distance} (threshold: {threshold})"
            )
        else:
            return ComparisonResult(
                filepath, False,
                f"hamming distance: {distance} (threshold: {threshold})"
            )

    elif mode == "rms":
        if not HAS_PILLOW:
            return ComparisonResult(
                filepath, False,
                "RMS comparison requires the 'Pillow' package"
            )
        # Load expected image
        expected_path = config.get("expected_image")
        if not expected_path:
            # Try conventional name: same stem + .expected.png
            stem = os.path.splitext(os.path.basename(filepath))[0]
            expected_path = os.path.join(os.path.dirname(filepath), f"{stem}.expected.png")

        if not os.path.isfile(expected_path):
            return ComparisonResult(
                filepath, False, f"expected image not found: {expected_path}"
            )

        from io import BytesIO

        actual_img = Image.open(BytesIO(resp.content))
        expected_img = Image.open(expected_path)
        rms = _compute_rms(actual_img, expected_img)

        if rms is None:
            return ComparisonResult(filepath, False, "RMS computation failed")

        if rms <= threshold:
            return ComparisonResult(
                filepath, True, f"RMS: {rms:.1f} (threshold: {threshold})"
            )
        else:
            return ComparisonResult(
                filepath, False, f"RMS: {rms:.1f} (threshold: {threshold})"
            )

    else:
        return ComparisonResult(filepath, False, f"unknown comparison mode: {mode}")


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def _print_header(pack_name):
    """Print the results header."""
    title = f"Test Results: {pack_name}"
    print(title)
    print("=" * len(title))


def _format_validation_line(vr):
    """Format a single validation result line."""
    if vr.passed:
        detail = f"{vr.node_count} nodes, {vr.output_count} output"
        if vr.output_count != 1:
            detail = f"{vr.node_count} nodes, {vr.output_count} outputs"
        return f"  PASS  {vr.filename} ({detail})"
    else:
        return f"  FAIL  {vr.filename} \u2014 {'; '.join(vr.errors)}"


def _format_execution_line(er):
    """Format a single execution result line."""
    if er.skipped:
        return f"  SKIP  {er.filename} ({er.message})"
    elif er.success:
        return f"  PASS  {er.filename} ({er.message})"
    else:
        return f"  FAIL  {er.filename} \u2014 {er.message}"


def _format_comparison_line(cr):
    """Format a single comparison result line."""
    if cr.success:
        return f"  PASS  {cr.filename} \u2014 {cr.message}"
    else:
        return f"  FAIL  {cr.filename} \u2014 {cr.message}"


def _build_json_report(pack_name, validation_results, execution_results, comparison_results):
    """Build a JSON-serialisable report dict."""
    report = {
        "pack": pack_name,
        "validation": [],
        "execution": [],
        "comparison": [],
        "summary": {"passed": 0, "failed": 0, "skipped": 0},
    }

    for vr in validation_results:
        entry = {
            "file": vr.filename,
            "passed": vr.passed,
            "nodes": vr.node_count,
            "outputs": vr.output_count,
        }
        if not vr.passed:
            entry["errors"] = vr.errors
        if vr.warnings:
            entry["warnings"] = vr.warnings
        report["validation"].append(entry)
        if vr.passed:
            report["summary"]["passed"] += 1
        else:
            report["summary"]["failed"] += 1

    for er in execution_results:
        entry = {
            "file": er.filename,
            "passed": er.success,
            "message": er.message,
            "elapsed": round(er.elapsed, 2),
            "skipped": er.skipped,
        }
        report["execution"].append(entry)
        if er.skipped:
            report["summary"]["skipped"] += 1
        elif er.success:
            report["summary"]["passed"] += 1
        else:
            report["summary"]["failed"] += 1

    for cr in comparison_results:
        entry = {
            "file": cr.filename,
            "passed": cr.success,
            "message": cr.message,
        }
        report["comparison"].append(entry)
        if cr.success:
            report["summary"]["passed"] += 1
        else:
            report["summary"]["failed"] += 1

    return report


# ---------------------------------------------------------------------------
# Main runner for a single pack
# ---------------------------------------------------------------------------

def run_pack(
    pack_name,
    tests_dir,
    url=None,
    api_key=None,
    timeout=DEFAULT_TIMEOUT,
    validate_only=False,
    compare=False,
    json_output=False,
):
    """Run all tests for a single pack. Returns (json_report, any_failed)."""

    validation_results = []
    execution_results = []
    comparison_results = []

    # Discover workflows
    workflows = _discover_workflows(tests_dir, pack_name)
    all_wf = workflows["api"] + workflows["e2e"]

    if not all_wf:
        if not json_output:
            _print_header(pack_name)
            print(f"\n  No test workflows found for '{pack_name}' in {tests_dir}")
            pack_dir = os.path.join(tests_dir, pack_name)
            print(f"  Looked in: {pack_dir}")
            # Show what directories exist
            if os.path.isdir(tests_dir):
                available = _discover_all_packs(tests_dir)
                if available:
                    print(f"  Available packs: {', '.join(available[:10])}")
                    if len(available) > 10:
                        print(f"    ... and {len(available) - 10} more")
            print()
        report = _build_json_report(pack_name, [], [], [])
        report["summary"]["failed"] = 1  # No workflows counts as failure
        return report, True

    # ----- Structure validation (always runs) -----
    failed_validation_files = set()
    for filepath in all_wf:
        vr = validate_workflow_extended(filepath)
        validation_results.append(vr)
        if not vr.passed:
            failed_validation_files.add(filepath)

    # ----- Execution (when --url provided and not --validate-only) -----
    if url and not validate_only:
        if not HAS_REQUESTS:
            if not json_output:
                print(
                    "\nERROR: 'requests' library required for execution mode. "
                    "Install with: pip install requests"
                )
            report = _build_json_report(pack_name, validation_results, [], [])
            report["summary"]["failed"] += 1
            return report, True

        session = _make_session(api_key)

        # Check connectivity
        reachable, err_msg = _check_comfyui_reachable(session, url)
        if not reachable:
            if not json_output:
                _print_header(pack_name)
                print(f"\nERROR: {err_msg}")
            report = _build_json_report(pack_name, validation_results, [], [])
            report["summary"]["failed"] += 1
            return report, True

        for filepath in all_wf:
            if filepath in failed_validation_files:
                execution_results.append(
                    ExecutionResult(filepath, False, "failed structure validation", skipped=True)
                )
                continue

            # Check for a config file that might specify input uploads
            config_path = filepath.replace(".json", ".config.json")
            if os.path.isfile(config_path):
                try:
                    with open(config_path, "r", encoding="utf-8") as f:
                        wf_config = json.load(f)
                except (json.JSONDecodeError, OSError):
                    wf_config = {}

                ok, upload_err = _upload_input_files(session, url, wf_config)
                if not ok:
                    execution_results.append(
                        ExecutionResult(filepath, False, f"input upload failed: {upload_err}")
                    )
                    continue

            er = execute_workflow(session, url, filepath, timeout=timeout)
            execution_results.append(er)

        # ----- E2E comparison (when --compare and e2e workflows exist) -----
        if compare and workflows["e2e"]:
            for filepath in workflows["e2e"]:
                config_path = filepath.replace(".json", ".config.json")
                if not os.path.isfile(config_path):
                    continue
                cr = run_e2e_comparison(session, url, filepath, config_path)
                comparison_results.append(cr)

    # ----- Build report -----
    report = _build_json_report(
        pack_name, validation_results, execution_results, comparison_results
    )

    # ----- Print human-readable output -----
    if not json_output:
        _print_header(pack_name)
        print()

        print("Structure Validation:")
        for vr in validation_results:
            print(_format_validation_line(vr))
            for w in vr.warnings:
                print(f"         WARN  {w}")

        if execution_results:
            print(f"\nExecution ({url}):")
            for er in execution_results:
                print(_format_execution_line(er))

        if comparison_results:
            print("\nE2E Comparison:")
            for cr in comparison_results:
                print(_format_comparison_line(cr))

        s = report["summary"]
        total = s["passed"] + s["failed"] + s["skipped"]
        parts = []
        parts.append(f"{s['passed']} passed")
        parts.append(f"{s['failed']} failed")
        if s["skipped"]:
            parts.append(f"{s['skipped']} skipped")
        print(f"\nSummary: {', '.join(parts)}")
        print()

    any_failed = report["summary"]["failed"] > 0
    return report, any_failed


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Local test runner for ComfyUI custom node test workflows",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Validate structure only (no ComfyUI needed)
  python scripts/run-local-tests.py --pack comfyui-example --validate-only

  # Execute against local ComfyUI
  python scripts/run-local-tests.py --pack comfyui-example --url http://localhost:8188

  # Execute against ephemeral env
  python scripts/run-local-tests.py --pack comfyui-example --url https://pr-123.testenvs.comfy.org --api-key $API_KEY

  # All packs, validate only
  python scripts/run-local-tests.py --all --validate-only

  # JSON output for CI
  python scripts/run-local-tests.py --pack comfyui-example --validate-only --json
        """,
    )

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--pack",
        help="Name of the node pack to test (directory name under tests dir)",
    )
    group.add_argument(
        "--all",
        action="store_true",
        help="Test all packs found in the tests directory",
    )

    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Only validate workflow structure (no ComfyUI needed)",
    )
    parser.add_argument(
        "--url",
        default=None,
        help="ComfyUI instance URL for execution (e.g. http://localhost:8188)",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="API key for authenticated ComfyUI instances",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help=f"Timeout in seconds per workflow execution (default: {DEFAULT_TIMEOUT})",
    )
    parser.add_argument(
        "--compare",
        action="store_true",
        help="Run E2E comparison for workflows with .config.json files",
    )
    parser.add_argument(
        "--tests-dir",
        default=None,
        help="Path to tests directory (auto-detected if not set)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output results as JSON (for CI integration)",
    )

    args = parser.parse_args()

    # Require --url when not in validate-only mode
    if not args.validate_only and not args.url:
        parser.error(
            "--url is required when not using --validate-only. "
            "Use --validate-only for structure checks without ComfyUI, "
            "or provide --url http://localhost:8188 to execute workflows."
        )

    tests_dir = _resolve_tests_dir(args.tests_dir)

    if args.all:
        packs = _discover_all_packs(tests_dir)
        if not packs:
            if args.json_output:
                print(json.dumps({"error": f"No packs found in {tests_dir}"}, indent=2))
            else:
                print(f"No packs found in {tests_dir}")
            sys.exit(1)
    else:
        packs = [args.pack]

    all_reports = []
    any_failed = False

    for pack_name in packs:
        report, failed = run_pack(
            pack_name=pack_name,
            tests_dir=tests_dir,
            url=args.url,
            api_key=args.api_key,
            timeout=args.timeout,
            validate_only=args.validate_only,
            compare=args.compare,
            json_output=args.json_output,
        )
        all_reports.append(report)
        if failed:
            any_failed = True

    if args.json_output:
        if len(all_reports) == 1:
            print(json.dumps(all_reports[0], indent=2))
        else:
            # Aggregate summary
            combined = {
                "packs": all_reports,
                "summary": {"passed": 0, "failed": 0, "skipped": 0},
            }
            for r in all_reports:
                for k in ("passed", "failed", "skipped"):
                    combined["summary"][k] += r["summary"][k]
            print(json.dumps(combined, indent=2))

    sys.exit(1 if any_failed else 0)


if __name__ == "__main__":
    main()
