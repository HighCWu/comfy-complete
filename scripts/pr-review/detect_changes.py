#!/usr/bin/env python3
"""Detect changes in supported_nodes.yaml between base and head branches.

This module compares the supported_nodes.yaml configuration between two git refs
to identify new, updated, and removed node packs for PR review.

Usage:
    python detect_changes.py --base origin/main --output changes.json
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class NodePack:
    """Represents a custom node pack configuration."""

    name: str
    version: str = ""
    node_labels: dict[str, list[str]] = field(default_factory=dict)
    web_directory: str = ""
    dependency_overrides: list[str] = field(default_factory=list)
    system_dependencies: list[str] = field(default_factory=list)
    models: list[dict[str, str]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> NodePack:
        """Create a NodePack from a dictionary."""
        return cls(
            name=data.get("name", ""),
            version=data.get("version", ""),
            node_labels=data.get("node_labels", {}),
            web_directory=data.get("web_directory", ""),
            dependency_overrides=data.get("dependency_overrides", []),
            system_dependencies=data.get("system_dependencies", []),
            models=data.get("models", []),
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        result = {
            "name": self.name,
            "version": self.version,
            "node_labels": self.node_labels,
            "web_directory": self.web_directory,
        }
        if self.dependency_overrides:
            result["dependency_overrides"] = self.dependency_overrides
        if self.system_dependencies:
            result["system_dependencies"] = self.system_dependencies
        if self.models:
            result["models"] = self.models
        return result


@dataclass
class LabelChanges:
    """Tracks changes to node labels between versions."""

    added: dict[str, list[str]] = field(default_factory=dict)
    removed: dict[str, list[str]] = field(default_factory=dict)
    modified: dict[str, dict[str, list[str]]] = field(default_factory=dict)


@dataclass
class ChangeReport:
    """Complete report of changes between base and head."""

    new: list[NodePack] = field(default_factory=list)
    updated: list[dict[str, Any]] = field(default_factory=list)
    removed: list[NodePack] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        """Check if any changes were detected."""
        return bool(self.new or self.updated or self.removed)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "new": [p.to_dict() for p in self.new],
            "updated": self.updated,
            "removed": [p.to_dict() for p in self.removed],
            "summary": {
                "new_count": len(self.new),
                "updated_count": len(self.updated),
                "removed_count": len(self.removed),
                "has_changes": self.has_changes,
            },
        }


def load_yaml_from_git(ref: str, file_path: str) -> dict[str, Any]:
    """Load YAML content from a specific git ref.

    Args:
        ref: Git reference (e.g., 'origin/main', 'HEAD')
        file_path: Path to the YAML file

    Returns:
        Parsed YAML content as dictionary, or empty dict with node_packs if not found
    """
    result = subprocess.run(
        ["git", "show", f"{ref}:{file_path}"],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        return {"node_packs": []}

    return yaml.safe_load(result.stdout) or {"node_packs": []}


def load_yaml_from_file(file_path: str) -> dict[str, Any]:
    """Load YAML content from a local file.

    Args:
        file_path: Path to the YAML file

    Returns:
        Parsed YAML content as dictionary, or empty dict with node_packs if not found
    """
    path = Path(file_path)
    if not path.exists():
        return {"node_packs": []}

    with path.open() as f:
        return yaml.safe_load(f) or {"node_packs": []}


def detect_label_changes(
    base_labels: dict[str, list[str]],
    head_labels: dict[str, list[str]],
) -> LabelChanges:
    """Detect changes in node labels between two versions.

    Args:
        base_labels: Labels from base version
        head_labels: Labels from head version

    Returns:
        LabelChanges object with added, removed, and modified labels
    """
    changes = LabelChanges()

    base_nodes = set(base_labels.keys())
    head_nodes = set(head_labels.keys())

    # New nodes with labels
    for node in head_nodes - base_nodes:
        changes.added[node] = head_labels[node]

    # Removed nodes with labels
    for node in base_nodes - head_nodes:
        changes.removed[node] = base_labels[node]

    # Modified labels
    for node in base_nodes & head_nodes:
        if set(base_labels[node]) != set(head_labels[node]):
            changes.modified[node] = {
                "old": base_labels[node],
                "new": head_labels[node],
            }

    return changes


def detect_changes(base_ref: str, config_path: str) -> ChangeReport:
    """Detect all changes between base ref and current working tree.

    Args:
        base_ref: Git reference to compare against (e.g., 'origin/main')
        config_path: Path to the supported_nodes.yaml file

    Returns:
        ChangeReport with all detected changes
    """
    base_config = load_yaml_from_git(base_ref, config_path)
    head_config = load_yaml_from_file(config_path)

    # Index packs by name for efficient lookup
    base_packs = {p["name"]: NodePack.from_dict(p) for p in base_config.get("node_packs", [])}
    head_packs = {p["name"]: NodePack.from_dict(p) for p in head_config.get("node_packs", [])}

    report = ChangeReport()

    # Find new packs
    for name in set(head_packs.keys()) - set(base_packs.keys()):
        report.new.append(head_packs[name])

    # Find removed packs
    for name in set(base_packs.keys()) - set(head_packs.keys()):
        report.removed.append(base_packs[name])

    # Find updated packs
    for name in set(head_packs.keys()) & set(base_packs.keys()):
        base_pack = base_packs[name]
        head_pack = head_packs[name]

        # Check if anything changed
        if base_pack.to_dict() != head_pack.to_dict():
            base_dict: dict[str, Any] = {
                "version": base_pack.version,
                "node_labels": base_pack.node_labels,
            }
            if base_pack.dependency_overrides:
                base_dict["dependency_overrides"] = base_pack.dependency_overrides

            head_dict: dict[str, Any] = {
                "version": head_pack.version,
                "node_labels": head_pack.node_labels,
            }
            if head_pack.dependency_overrides:
                head_dict["dependency_overrides"] = head_pack.dependency_overrides

            update_info: dict[str, Any] = {
                "name": name,
                "base": base_dict,
                "head": head_dict,
            }

            # Check for version change
            if base_pack.version != head_pack.version:
                update_info["version_changed"] = True

            # Check for label changes
            if base_pack.node_labels != head_pack.node_labels:
                label_changes = detect_label_changes(
                    base_pack.node_labels,
                    head_pack.node_labels,
                )
                update_info["label_changes"] = {
                    "added": label_changes.added,
                    "removed": label_changes.removed,
                    "modified": label_changes.modified,
                }

            report.updated.append(update_info)

    return report


def main() -> int:
    """Main entry point for the script."""
    parser = argparse.ArgumentParser(
        description="Detect changes in supported_nodes.yaml",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--base",
        default="origin/main",
        help="Base git ref to compare against (default: origin/main)",
    )
    parser.add_argument(
        "--config",
        default="supported_nodes.yaml",
        help="Path to config file (default: supported_nodes.yaml)",
    )
    parser.add_argument(
        "--output",
        help="Output file path (default: stdout)",
    )
    args = parser.parse_args()

    report = detect_changes(args.base, args.config)
    output = json.dumps(report.to_dict(), indent=2)

    if args.output:
        Path(args.output).write_text(output)
    else:
        print(output)

    # Always return 0 - workflow checks has_changes in JSON output
    return 0


if __name__ == "__main__":
    sys.exit(main())
