#!/usr/bin/env python3
"""Check test coverage for node packs in supported_nodes.yaml.

For each node pack, checks that every non-exempt node has at least one
test workflow that references its class_type. Nodes with behavioral labels
that exempt them from execution (RequiresWebcam, RequiresDisplay,
RequiresClipboard, Incompatible, BrokenNode) are excluded from coverage
requirements.
"""

import argparse
import json
import os
import sys

import yaml

# Labels that exempt a node from test coverage requirements
EXEMPT_LABELS = {
    "RequiresWebcam",
    "RequiresDisplay",
    "RequiresClipboard",
    "Incompatible",
    "BrokenNode",
}


def normalize_pack_name(name):
    """Normalize a pack name for directory matching.

    URL-style names like 'https://github.com/user/Repo@sha' become 'Repo'.
    Regular names are returned as-is.
    """
    if "/" in name:
        base = name.split("@")[0] if "@" in name else name
        base = base.rstrip("/").split("/")[-1]
        return base
    return name


def load_supported_nodes(supported_nodes_path):
    """Load node packs and their labeled nodes from supported_nodes.yaml.

    Returns a dict: {pack_name: {normalized_name, node_labels: {node: [labels]}}}
    """
    with open(supported_nodes_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    packs = {}
    for pack in data.get("node_packs", []):
        name = pack.get("name", "")
        normalized = normalize_pack_name(name)
        node_labels = pack.get("node_labels", {}) or {}
        packs[name] = {
            "normalized_name": normalized,
            "node_labels": node_labels,
        }
    return packs


def get_exempt_nodes(node_labels):
    """Return set of node names that are exempt from coverage based on labels."""
    exempt = set()
    for node_name, labels in node_labels.items():
        if labels and any(label in EXEMPT_LABELS for label in labels):
            exempt.add(node_name)
    return exempt


def collect_tested_class_types(tests_dir, pack_dir_name):
    """Collect all class_types referenced in test workflow JSONs for a pack."""
    class_types = set()
    pack_path = os.path.join(tests_dir, pack_dir_name)

    if not os.path.isdir(pack_path):
        return class_types

    for filename in os.listdir(pack_path):
        if not filename.endswith(".json"):
            continue
        filepath = os.path.join(pack_path, filename)
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue

        if not isinstance(data, dict):
            continue

        for node_config in data.values():
            if isinstance(node_config, dict) and "class_type" in node_config:
                class_types.add(node_config["class_type"])

    return class_types


def collect_all_tested_class_types(tests_dir):
    """Collect all class_types from all test workflows across all packs."""
    all_class_types = set()
    if not os.path.isdir(tests_dir):
        return all_class_types

    for pack_dir in os.listdir(tests_dir):
        pack_path = os.path.join(tests_dir, pack_dir)
        if not os.path.isdir(pack_path):
            continue
        all_class_types.update(collect_tested_class_types(tests_dir, pack_dir))

    return all_class_types


def check_coverage(supported_nodes_path, tests_dir, report=False, pack_filter=None):
    """Check test coverage and return (covered, gaps, exempt) counts.

    Returns: (total_packs_checked, packs_with_gaps, gap_details)
    gap_details is a list of (pack_name, missing_nodes) tuples.
    """
    packs = load_supported_nodes(supported_nodes_path)
    all_tested = collect_all_tested_class_types(tests_dir)

    total_nodes = 0
    covered_nodes = 0
    exempt_nodes_count = 0
    gap_details = []

    for pack_name, pack_info in sorted(packs.items()):
        normalized = pack_info["normalized_name"]
        node_labels = pack_info["node_labels"]

        # If filtering by pack, skip non-matching packs
        if pack_filter and normalized != pack_filter and pack_name != pack_filter:
            continue

        # Get class_types tested for this specific pack directory
        pack_tested = collect_tested_class_types(tests_dir, normalized)

        # Get exempt nodes
        exempt = get_exempt_nodes(node_labels)

        # All labeled nodes (we can only check coverage for nodes we know about)
        all_labeled = set(node_labels.keys()) if node_labels else set()

        # Nodes that need tests = labeled but not exempt
        needs_tests = all_labeled - exempt

        # For labeled nodes, check if they appear in any test workflow
        missing = set()
        for node_name in needs_tests:
            if node_name not in pack_tested and node_name not in all_tested:
                missing.add(node_name)

        total_nodes += len(all_labeled)
        exempt_nodes_count += len(exempt)
        covered_nodes += len(needs_tests) - len(missing)

        if report:
            print(f"\n{'='*60}")
            print(f"Pack: {pack_name}")
            print(f"  Directory: tests/node-tests/{normalized}/")
            test_count = 0
            pack_path = os.path.join(tests_dir, normalized)
            if os.path.isdir(pack_path):
                test_count = len([f for f in os.listdir(pack_path) if f.endswith(".json")])
            print(f"  Test files: {test_count}")
            print(f"  Labeled nodes: {len(all_labeled)}")
            print(f"  Exempt nodes: {len(exempt)}")

            if exempt:
                for node in sorted(exempt):
                    labels_str = ", ".join(node_labels.get(node, []))
                    print(f"    - {node} [{labels_str}]")

            print(f"  Nodes needing tests: {len(needs_tests)}")
            print(f"  Covered: {len(needs_tests) - len(missing)}")
            print(f"  Missing: {len(missing)}")

            if missing:
                for node in sorted(missing):
                    labels_str = ""
                    if node in node_labels and node_labels[node]:
                        labels_str = f" [{', '.join(node_labels[node])}]"
                    print(f"    - {node}{labels_str}")

        if missing:
            gap_details.append((pack_name, sorted(missing)))

    testable = total_nodes - exempt_nodes_count
    if report:
        print(f"\n{'='*60}")
        print("SUMMARY")
        print(f"  Total labeled nodes: {total_nodes}")
        print(f"  Exempt from testing: {exempt_nodes_count}")
        print(f"  Testable nodes: {testable}")
        print(f"  Covered: {covered_nodes}")
        print(f"  Missing coverage: {testable - covered_nodes}")

        if gap_details:
            print(f"\n  Packs with gaps: {len(gap_details)}")
        else:
            print("\n  All testable nodes have coverage!")

    return total_nodes, gap_details


def main():
    parser = argparse.ArgumentParser(description="Check test coverage for node packs")
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
        "--report",
        action="store_true",
        help="Print a detailed human-readable report",
    )
    parser.add_argument(
        "--pack",
        default=None,
        help="Check coverage for a specific pack only",
    )
    parser.add_argument(
        "--warn-only",
        action="store_true",
        help="Print warnings but exit 0 even if gaps exist",
    )
    args = parser.parse_args()

    total, gap_details = check_coverage(
        args.supported_nodes,
        args.tests_dir,
        report=args.report,
        pack_filter=args.pack,
    )

    if gap_details:
        if not args.report:
            # Print summary if not already printed
            print("Coverage gaps found:")
            for pack_name, missing in gap_details:
                print(f"  {pack_name}: {len(missing)} node(s) missing tests")
                for node in missing:
                    print(f"    - {node}")

        if args.warn_only:
            print("\n(--warn-only: exiting with code 0)")
            sys.exit(0)
        else:
            sys.exit(1)
    else:
        if not args.report:
            print("All testable nodes have test coverage.")
        sys.exit(0)


if __name__ == "__main__":
    main()
