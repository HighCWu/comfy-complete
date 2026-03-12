#!/usr/bin/env python3
"""Validate test workflow JSON files for ComfyUI API format correctness.

Checks that each test workflow JSON file:
- Is valid JSON
- Is a non-empty dict of node IDs mapping to node configs
- Each node has required keys: class_type, inputs
- Node pack directories correspond to packs declared in supported_nodes.yaml
"""

import argparse
import json
import os
import sys

import yaml


def load_supported_packs(supported_nodes_path):
    """Load node pack names from supported_nodes.yaml."""
    with open(supported_nodes_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    packs = set()
    for pack in data.get("node_packs", []):
        name = pack.get("name", "")
        # Normalize: use the last path component for URL-style names
        if "/" in name:
            # e.g. "https://github.com/user/Repo@sha" -> "Repo"
            # Strip @commit suffix first
            base = name.split("@")[0] if "@" in name else name
            base = base.rstrip("/").split("/")[-1]
            packs.add(base)
        packs.add(name)
    return packs


def validate_workflow(filepath):
    """Validate a single workflow JSON file. Returns list of error strings."""
    errors = []

    # Check valid JSON
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        errors.append(f"Invalid JSON: {e}")
        return errors

    # Must be a dict
    if not isinstance(data, dict):
        errors.append(f"Top level must be a dict, got {type(data).__name__}")
        return errors

    # Must not be empty
    if len(data) == 0:
        errors.append("Workflow is empty (no nodes)")
        return errors

    # Each node must have class_type and inputs
    for node_id, node_config in data.items():
        if not isinstance(node_config, dict):
            errors.append(f"Node '{node_id}': config must be a dict, got {type(node_config).__name__}")
            continue

        if "class_type" not in node_config:
            errors.append(f"Node '{node_id}': missing required key 'class_type'")

        if "inputs" not in node_config:
            errors.append(f"Node '{node_id}': missing required key 'inputs'")

    return errors


def find_test_workflows(tests_dir):
    """Find all test workflow JSON files under the tests directory."""
    workflows = []
    if not os.path.isdir(tests_dir):
        return workflows

    for pack_dir in sorted(os.listdir(tests_dir)):
        pack_path = os.path.join(tests_dir, pack_dir)
        if not os.path.isdir(pack_path):
            continue
        for filename in sorted(os.listdir(pack_path)):
            if filename.endswith(".json"):
                workflows.append(os.path.join(pack_path, filename))
    return workflows


def main():
    parser = argparse.ArgumentParser(description="Validate test workflow JSON files")
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
    args = parser.parse_args()

    tests_dir = args.tests_dir
    supported_nodes_path = args.supported_nodes

    # Load supported packs for cross-referencing
    supported_packs = set()
    if os.path.isfile(supported_nodes_path):
        supported_packs = load_supported_packs(supported_nodes_path)

    workflows = find_test_workflows(tests_dir)
    if not workflows:
        print(f"No test workflow JSON files found in {tests_dir}")
        sys.exit(1)

    total = 0
    passed = 0
    failed = 0
    warnings = []

    # Check for test directories that don't match any known pack
    if supported_packs and os.path.isdir(tests_dir):
        for pack_dir in sorted(os.listdir(tests_dir)):
            pack_path = os.path.join(tests_dir, pack_dir)
            if not os.path.isdir(pack_path):
                continue
            # Check if directory name matches any known pack name (exact or repo basename)
            if pack_dir not in supported_packs:
                warnings.append(f"WARNING: Test directory '{pack_dir}' does not match any pack in supported_nodes.yaml")

    for filepath in workflows:
        total += 1
        rel_path = os.path.relpath(filepath, tests_dir)
        errors = validate_workflow(filepath)

        if errors:
            failed += 1
            print(f"FAIL: {rel_path}")
            for err in errors:
                print(f"  - {err}")
        else:
            passed += 1
            print(f"  OK: {rel_path}")

    print()
    for w in warnings:
        print(w)

    if warnings:
        print()

    print(f"Results: {passed}/{total} passed, {failed} failed")

    if failed > 0:
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
