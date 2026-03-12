#!/usr/bin/env python3
"""
Comfy Complete - Resolve Disabled Nodes

Reads supported_nodes.yaml and config.yaml to produce a list of all nodes
that should be disabled based on the label filtering configuration.

Usage:
    ./scripts/resolve_disabled_nodes.py                     # Use defaults
    ./scripts/resolve_disabled_nodes.py --config config.yaml
    ./scripts/resolve_disabled_nodes.py --format json       # Output as JSON
    ./scripts/resolve_disabled_nodes.py --format list       # One node per line (default)
    ./scripts/resolve_disabled_nodes.py --validate          # Validate labels are declared
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml


def load_yaml(path: Path) -> dict[str, Any]:
    """Load and parse a YAML file."""
    with open(path) as f:
        return yaml.safe_load(f) or {}


def get_node_labels(supported_nodes: dict[str, Any]) -> dict[str, set[str]]:
    """
    Build a mapping of node_name -> set of labels from supported_nodes.yaml.
    """
    node_labels: dict[str, set[str]] = {}

    for node_pack in supported_nodes.get("node_packs", []):
        labels_map = node_pack.get("node_labels", {})
        for node_name, labels in labels_map.items():
            if node_name not in node_labels:
                node_labels[node_name] = set()
            node_labels[node_name].update(labels)

    return node_labels


def resolve_filter(
    node_labels: dict[str, set[str]],
    filter_config: dict[str, Any],
) -> set[str]:
    """
    Apply the filter to find disabled nodes.

    The filter uses OR logic - a node is disabled if it matches ANY condition.
    """
    or_conditions = filter_config.get("or", [])

    if not or_conditions:
        return set()

    disabled_nodes: set[str] = set()

    for node_name, labels in node_labels.items():
        for condition in or_conditions:
            # Each condition is a dict like {"ReadsArbitraryFile": true}
            for label, required_value in condition.items():
                has_label = label in labels
                if required_value is True and has_label:
                    disabled_nodes.add(node_name)
                elif required_value is False and not has_label:
                    disabled_nodes.add(node_name)

    return disabled_nodes


def get_all_disabled_nodes(
    supported_nodes: dict[str, Any],
    filter_config: dict[str, Any] | None = None,
) -> list[str]:
    """
    Get complete list of disabled nodes combining:
    1. Static disallow_nodes from each pack
    2. Dynamic label-based filtering
    """
    disabled: set[str] = set()

    # Add static disallow_nodes
    for pack in supported_nodes.get("node_packs", []):
        disabled.update(pack.get("disallow_nodes", []))

    # Add label-filtered nodes
    if filter_config:
        node_labels = get_node_labels(supported_nodes)
        disabled.update(resolve_filter(node_labels, filter_config))

    return sorted(disabled)


def validate_labels(supported_nodes: dict[str, Any]) -> list[str]:
    """
    Validate that all labels used in node_packs are declared in the labels list.
    Returns a list of validation errors.
    """
    declared_labels = set(supported_nodes.get("labels", []))
    errors: list[str] = []

    for node_pack in supported_nodes.get("node_packs", []):
        pack_name = node_pack.get("name", "unknown")
        labels_map = node_pack.get("node_labels", {})

        for node_name, labels in labels_map.items():
            for label in labels:
                if label not in declared_labels:
                    errors.append(
                        f"Undeclared label '{label}' used on node '{node_name}' "
                        f"in pack '{pack_name}'"
                    )

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Resolve disabled nodes based on label filters"
    )
    parser.add_argument(
        "--supported-nodes",
        type=Path,
        default=None,
        help="Path to supported_nodes.yaml",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to config.yaml",
    )
    parser.add_argument(
        "--format",
        choices=["list", "json"],
        default="list",
        help="Output format (default: list)",
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Validate labels are declared before resolving",
    )
    parser.add_argument(
        "--include-static",
        action="store_true",
        help="Include static disallow_nodes in output (default: only label-filtered)",
    )
    args = parser.parse_args()

    # Find files
    script_dir = Path(__file__).parent
    repo_root = script_dir.parent

    supported_nodes_path = (
        args.supported_nodes if args.supported_nodes else repo_root / "supported_nodes.yaml"
    )
    config_path = args.config if args.config else repo_root / "config.yaml"

    if not supported_nodes_path.exists():
        print(f"Error: supported_nodes.yaml not found at {supported_nodes_path}", file=sys.stderr)
        return 1

    supported_nodes = load_yaml(supported_nodes_path)

    # Validate labels if requested
    if args.validate:
        errors = validate_labels(supported_nodes)
        if errors:
            print("Label validation errors:", file=sys.stderr)
            for error in errors:
                print(f"  - {error}", file=sys.stderr)
            return 1
        print("Labels valid.")
        return 0

    # Load config (optional - if not present, return empty filter)
    config = load_yaml(config_path) if config_path.exists() else {}
    filter_config = config.get("disable_nodes", {})

    # Resolve disabled nodes
    if args.include_static:
        disabled = get_all_disabled_nodes(supported_nodes, filter_config)
    else:
        node_labels = get_node_labels(supported_nodes)
        disabled = sorted(resolve_filter(node_labels, filter_config))

    # Output
    if args.format == "json":
        print(json.dumps(disabled, indent=2))
    else:
        for node in disabled:
            print(node)

    return 0


if __name__ == "__main__":
    sys.exit(main())
