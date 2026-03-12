#!/usr/bin/env python3
"""Local test runner for validating custom node test workflow submissions.

Validates test workflow JSON structure and checks test coverage for a
specified node pack. Optionally executes workflows against a local
ComfyUI instance.

Usage:
    python scripts/run_tests.py --pack comfyui-inpaint-nodes
    python scripts/run_tests.py --pack comfyui-inpaint-nodes --execute --comfyui-url http://localhost:8188
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request


# Reuse logic from sibling scripts by importing their functions.
# We add the scripts directory to sys.path so imports work regardless
# of where the script is invoked from.
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from validate_test_workflows import find_test_workflows, validate_workflow  # noqa: E402
from check_test_coverage import check_coverage  # noqa: E402


def get_pack_test_dir(tests_dir, pack_name):
    """Return the test directory for a given pack name."""
    return os.path.join(tests_dir, pack_name)


def run_validation(tests_dir, pack_name):
    """Validate all workflow JSON files for the given pack.

    Returns (passed, failed) counts.
    """
    pack_dir = get_pack_test_dir(tests_dir, pack_name)

    if not os.path.isdir(pack_dir):
        print(f"\n  No test directory found at: {pack_dir}")
        return 0, 0

    workflows = []
    for filename in sorted(os.listdir(pack_dir)):
        if filename.endswith(".json"):
            workflows.append(os.path.join(pack_dir, filename))

    if not workflows:
        print(f"\n  No test workflow JSON files found in {pack_dir}")
        return 0, 0

    passed = 0
    failed = 0

    for filepath in workflows:
        rel_path = os.path.relpath(filepath, tests_dir)
        errors = validate_workflow(filepath)

        if errors:
            failed += 1
            print(f"  FAIL: {rel_path}")
            for err in errors:
                print(f"    - {err}")
        else:
            passed += 1
            print(f"    OK: {rel_path}")

    return passed, failed


def execute_workflow(comfyui_url, filepath, timeout=120):
    """Execute a workflow against a ComfyUI instance.

    POSTs the workflow to /api/prompt, then polls /api/history for completion.

    Returns (success: bool, message: str).
    """
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            workflow = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        return False, f"Failed to load workflow: {e}"

    # POST to /api/prompt
    prompt_url = f"{comfyui_url.rstrip('/')}/api/prompt"
    payload = json.dumps({"prompt": workflow}).encode("utf-8")

    req = urllib.request.Request(
        prompt_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp_data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        return False, f"Failed to submit workflow: {e}"
    except Exception as e:
        return False, f"Failed to submit workflow: {e}"

    prompt_id = resp_data.get("prompt_id")
    if not prompt_id:
        return False, f"No prompt_id in response: {resp_data}"

    # Poll /api/history/{prompt_id} for completion
    history_url = f"{comfyui_url.rstrip('/')}/api/history/{prompt_id}"
    start_time = time.time()

    while time.time() - start_time < timeout:
        time.sleep(2)

        try:
            with urllib.request.urlopen(history_url, timeout=10) as resp:
                history = json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError:
            continue
        except Exception:
            continue

        if prompt_id in history:
            entry = history[prompt_id]
            status = entry.get("status", {})
            completed = status.get("completed", False)
            status_str = status.get("status_str", "unknown")

            if completed:
                # Check for errors in outputs
                outputs = entry.get("outputs", {})
                has_errors = False
                for node_id, node_output in outputs.items():
                    if "errors" in node_output:
                        has_errors = True
                        break

                if has_errors:
                    return False, f"Execution completed with errors (status: {status_str})"

                return True, f"Execution completed successfully (status: {status_str})"

            if status_str == "error":
                messages = status.get("messages", [])
                error_msg = "; ".join(str(m) for m in messages) if messages else "unknown error"
                return False, f"Execution failed: {error_msg}"

    return False, f"Execution timed out after {timeout}s"


def main():
    parser = argparse.ArgumentParser(
        description="Local test runner for custom node test workflows",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/run_tests.py --pack comfyui-inpaint-nodes
  python scripts/run_tests.py --pack comfyui-kjnodes --execute --comfyui-url http://localhost:8188
        """,
    )
    parser.add_argument(
        "--pack",
        required=True,
        help="Name of the node pack to test (directory name under tests/node-tests/)",
    )
    parser.add_argument(
        "--tests-dir",
        default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests", "node-tests"),
        help="Path to node-tests directory",
    )
    parser.add_argument(
        "--supported-nodes",
        default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "supported_nodes.yaml"),
        help="Path to supported_nodes.yaml",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Execute workflows against a ComfyUI instance",
    )
    parser.add_argument(
        "--comfyui-url",
        default="http://localhost:8188",
        help="ComfyUI instance URL (default: http://localhost:8188)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=120,
        help="Timeout in seconds for each workflow execution (default: 120)",
    )
    args = parser.parse_args()

    if args.execute and not args.comfyui_url:
        parser.error("--comfyui-url is required when --execute is used")

    all_passed = True

    # Step 1: Validate workflow structure
    print(f"[1/3] Validating workflow structure for '{args.pack}'...")
    v_passed, v_failed = run_validation(args.tests_dir, args.pack)
    print(f"      Validation: {v_passed} passed, {v_failed} failed")

    if v_failed > 0:
        all_passed = False

    # Step 2: Check test coverage
    print(f"\n[2/3] Checking test coverage for '{args.pack}'...")
    _, gap_details = check_coverage(
        args.supported_nodes,
        args.tests_dir,
        report=False,
        pack_filter=args.pack,
    )

    if gap_details:
        all_passed = False
        for pack_name, missing in gap_details:
            print(f"      Coverage gaps in {pack_name}: {len(missing)} node(s) missing")
            for node in missing:
                print(f"        - {node}")
    else:
        print("      Coverage: all testable nodes covered")

    # Step 3: Execute workflows (optional)
    if args.execute:
        print(f"\n[3/3] Executing workflows against {args.comfyui_url}...")
        pack_dir = get_pack_test_dir(args.tests_dir, args.pack)

        if not os.path.isdir(pack_dir):
            print(f"      No test directory found: {pack_dir}")
        else:
            workflows = sorted(
                os.path.join(pack_dir, f)
                for f in os.listdir(pack_dir)
                if f.endswith(".json")
            )

            exec_passed = 0
            exec_failed = 0

            for filepath in workflows:
                filename = os.path.basename(filepath)
                print(f"      Running {filename}...", end=" ", flush=True)

                success, message = execute_workflow(args.comfyui_url, filepath, timeout=args.timeout)

                if success:
                    exec_passed += 1
                    print(f"PASS ({message})")
                else:
                    exec_failed += 1
                    all_passed = False
                    print(f"FAIL ({message})")

            print(f"      Execution: {exec_passed} passed, {exec_failed} failed")
    else:
        print("\n[3/3] Skipping execution (use --execute to run against ComfyUI)")

    # Final summary
    print()
    if all_passed:
        print("All checks passed.")
        sys.exit(0)
    else:
        print("Some checks failed.")
        sys.exit(1)


if __name__ == "__main__":
    main()
