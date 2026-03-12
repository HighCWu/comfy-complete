#!/usr/bin/env python3
"""Extract dependency overrides from supported_nodes.yaml and merge with requirements.txt.

Reads `dependency_overrides` from each node pack in supported_nodes.yaml, compares
them against an existing requirements.txt, and reports new dependencies, version
conflicts, and resolution suggestions.

Usage:
    python scripts/extract_deps.py --yaml supported_nodes.yaml --requirements requirements.txt
    python scripts/extract_deps.py --yaml supported_nodes.yaml --requirements requirements.txt --apply
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class ParsedRequirement:
    """A parsed pip requirement line."""

    name: str
    version_spec: str  # e.g., "==1.2.3", ">=1.0", ""
    full_line: str
    normalized_name: str = ""

    def __post_init__(self) -> None:
        self.normalized_name = normalize_package_name(self.name)


@dataclass
class MergeReport:
    """Report of merging dependency overrides with requirements.txt."""

    new_deps: list[tuple[str, str]] = field(default_factory=list)  # (dep_line, pack_name)
    conflicts: list[dict[str, str]] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)


def normalize_package_name(name: str) -> str:
    """Normalize a Python package name for comparison.

    PEP 503: all comparisons should be case-insensitive and treat
    hyphens, underscores, and periods as equivalent.
    """
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_requirement(line: str) -> ParsedRequirement | None:
    """Parse a single pip requirement line into its components.

    Returns None for comments, blank lines, and URL-based requirements.
    """
    line = line.strip()
    if not line or line.startswith("#"):
        return None

    # Skip git+/http URL requirements
    if line.startswith("git+") or line.startswith("http"):
        return None

    # Skip PEP 440 URL requirements (e.g., "package @ https://...")
    if " @ " in line:
        return None

    # Extract name and version spec
    # Match patterns like: package==1.0, package>=1.0, package~=1.0, package
    match = re.match(r"^([A-Za-z0-9][\w.\-]*)\s*(.*)", line)
    if not match:
        return None

    name = match.group(1)
    version_spec = match.group(2).strip()

    return ParsedRequirement(name=name, version_spec=version_spec, full_line=line)


def load_requirements(path: Path) -> dict[str, ParsedRequirement]:
    """Load and parse a requirements.txt file.

    Returns a dict mapping normalized package names to parsed requirements.
    """
    if not path.exists():
        return {}

    requirements: dict[str, ParsedRequirement] = {}
    content = path.read_text(encoding="utf-8")

    for line in content.splitlines():
        parsed = parse_requirement(line)
        if parsed:
            requirements[parsed.normalized_name] = parsed

    return requirements


def load_dependency_overrides(yaml_path: Path) -> list[tuple[str, str]]:
    """Load all dependency_overrides from supported_nodes.yaml.

    Returns a list of (dependency_line, pack_name) tuples.
    """
    with open(yaml_path, encoding="utf-8") as f:
        data: dict[str, Any] = yaml.safe_load(f) or {}

    overrides: list[tuple[str, str]] = []

    for pack in data.get("node_packs", []):
        pack_name = pack.get("name", "unknown")
        dep_overrides = pack.get("dependency_overrides", [])

        for dep in dep_overrides:
            if isinstance(dep, str):
                overrides.append((dep.strip(), pack_name))

    return overrides


def merge_dependencies(
    existing: dict[str, ParsedRequirement],
    overrides: list[tuple[str, str]],
) -> MergeReport:
    """Merge dependency overrides with existing requirements.

    Detects new dependencies, version conflicts, and unchanged matches.
    """
    report = MergeReport()

    for dep_line, pack_name in overrides:
        parsed = parse_requirement(dep_line)
        if not parsed:
            continue

        if parsed.normalized_name in existing:
            existing_req = existing[parsed.normalized_name]

            # Compare version specs
            if parsed.version_spec and existing_req.version_spec:
                if parsed.version_spec != existing_req.version_spec:
                    report.conflicts.append({
                        "package": parsed.name,
                        "existing": existing_req.full_line,
                        "override": dep_line,
                        "pack": pack_name,
                        "suggestion": (
                            f"Check if '{dep_line}' (from {pack_name}) is compatible "
                            f"with '{existing_req.full_line}' in requirements.txt. "
                            f"If the override needs a different version, update "
                            f"requirements.txt to the version that satisfies both."
                        ),
                    })
                else:
                    report.unchanged.append(
                        f"{parsed.name} ({parsed.version_spec}) - already in requirements.txt"
                    )
            else:
                report.unchanged.append(
                    f"{parsed.name} - already in requirements.txt"
                )
        else:
            report.new_deps.append((dep_line, pack_name))

    return report


def apply_merge(
    requirements_path: Path,
    new_deps: list[tuple[str, str]],
) -> None:
    """Append new dependencies to requirements.txt."""
    if not new_deps:
        return

    content = requirements_path.read_text(encoding="utf-8") if requirements_path.exists() else ""

    # Ensure file ends with newline
    if content and not content.endswith("\n"):
        content += "\n"

    # Add new deps with comment indicating source
    content += "\n# Dependencies added from supported_nodes.yaml dependency_overrides\n"
    for dep_line, pack_name in new_deps:
        content += f"{dep_line}  # from {pack_name}\n"

    requirements_path.write_text(content, encoding="utf-8")


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Extract and merge dependency overrides from supported_nodes.yaml",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python scripts/extract_deps.py --yaml supported_nodes.yaml "
            "--requirements requirements.txt\n"
            "  python scripts/extract_deps.py --yaml supported_nodes.yaml "
            "--requirements requirements.txt --apply\n"
        ),
    )
    parser.add_argument(
        "--yaml",
        type=Path,
        required=True,
        help="Path to supported_nodes.yaml",
    )
    parser.add_argument(
        "--requirements",
        type=Path,
        required=True,
        help="Path to requirements.txt",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write new dependencies to requirements.txt",
    )
    args = parser.parse_args()

    if not args.yaml.exists():
        print(f"Error: File not found: {args.yaml}", file=sys.stderr)
        return 1

    # Load data
    existing = load_requirements(args.requirements)
    overrides = load_dependency_overrides(args.yaml)

    if not overrides:
        print("No dependency_overrides found in supported_nodes.yaml")
        return 0

    print(f"Found {len(overrides)} dependency override(s) across node packs\n")

    # Merge
    report = merge_dependencies(existing, overrides)

    # Report results
    if report.new_deps:
        print(f"NEW DEPENDENCIES ({len(report.new_deps)}):")
        for dep_line, pack_name in report.new_deps:
            print(f"  + {dep_line}  (from {pack_name})")
        print()

    if report.conflicts:
        print(f"VERSION CONFLICTS ({len(report.conflicts)}):")
        for conflict in report.conflicts:
            print(f"  ! {conflict['package']}:")
            print(f"    requirements.txt: {conflict['existing']}")
            print(f"    override:         {conflict['override']} (from {conflict['pack']})")
            print(f"    suggestion:       {conflict['suggestion']}")
        print()

    if report.unchanged:
        print(f"UNCHANGED ({len(report.unchanged)}):")
        for msg in report.unchanged:
            print(f"  = {msg}")
        print()

    # Apply if requested
    if args.apply and report.new_deps:
        if not args.requirements.exists():
            print(f"Error: Cannot apply - {args.requirements} does not exist", file=sys.stderr)
            return 1
        apply_merge(args.requirements, report.new_deps)
        print(f"Applied {len(report.new_deps)} new dependencies to {args.requirements}")

    # Return non-zero if there are conflicts
    if report.conflicts:
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
