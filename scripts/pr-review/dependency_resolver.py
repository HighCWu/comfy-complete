#!/usr/bin/env python3
"""Dependency resolver for custom node packs.

Checks for dependency conflicts when adding a new node pack by merging
dependency_overrides from a PR with the existing requirements.txt.

Usage:
    python dependency_resolver.py --overrides "torch>=2.0" "numpy<2.0" --requirements requirements.txt
    python dependency_resolver.py --overrides-file overrides.txt --requirements requirements.txt
    python dependency_resolver.py --changes changes.json --requirements requirements.txt
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


def parse_requirement_name(req: str) -> str:
    """Extract the package name from a requirement string.

    Args:
        req: A requirement string like 'numpy>=1.20' or 'torch[cuda]>=2.0'.

    Returns:
        The normalized package name.
    """
    # Remove extras, version specifiers, and comments
    name = re.split(r"[>=<!\[;#]", req.strip())[0].strip()
    return name.lower().replace("-", "_")


def load_requirements(req_path: Path) -> list[str]:
    """Load requirements from a file, filtering comments and blank lines.

    Args:
        req_path: Path to the requirements file.

    Returns:
        List of requirement strings.
    """
    if not req_path.exists():
        return []

    reqs: list[str] = []
    try:
        for line in req_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and not line.startswith("-"):
                reqs.append(line)
    except OSError:
        pass
    return reqs


def extract_overrides_from_changes(changes_path: Path) -> list[str]:
    """Extract dependency_overrides from a changes.json file.

    Looks at both new and updated node packs for dependency_overrides.

    Args:
        changes_path: Path to the changes.json file.

    Returns:
        Combined list of dependency override strings.
    """
    overrides: list[str] = []
    try:
        data = json.loads(changes_path.read_text(encoding="utf-8"))
        for node in data.get("new", []):
            overrides.extend(node.get("dependency_overrides", []))
        for node in data.get("updated", []):
            head = node.get("head", {})
            overrides.extend(head.get("dependency_overrides", []))
    except (OSError, json.JSONDecodeError, KeyError):
        pass
    return overrides


def find_version_conflicts(
    existing_reqs: list[str], overrides: list[str]
) -> list[dict[str, str]]:
    """Find direct version conflicts between existing requirements and overrides.

    This does a simple string-based conflict detection by comparing version
    specifiers for the same package.

    Args:
        existing_reqs: List of existing requirement strings.
        overrides: List of override requirement strings.

    Returns:
        List of conflict dicts with package, existing, and override info.
    """
    conflicts: list[dict[str, str]] = []

    # Index existing requirements by normalized name
    existing_by_name: dict[str, str] = {}
    for req in existing_reqs:
        name = parse_requirement_name(req)
        if name:
            existing_by_name[name] = req

    for override in overrides:
        name = parse_requirement_name(override)
        if name and name in existing_by_name:
            existing_req = existing_by_name[name]
            # Only report if they differ (simple string comparison)
            if existing_req.strip() != override.strip():
                conflicts.append({
                    "package": name,
                    "existing": existing_req.strip(),
                    "override": override.strip(),
                    "type": "version_mismatch",
                })

    return conflicts


def try_uv_resolve(merged_reqs: list[str]) -> dict[str, Any]:
    """Try to resolve dependencies using uv pip compile.

    Args:
        merged_reqs: Combined list of requirement strings.

    Returns:
        Dict with resolution result: {tool, success, output, error}.
    """
    uv_path = shutil.which("uv")
    if not uv_path:
        return {"tool": "uv", "available": False}

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    ) as f:
        f.write("\n".join(merged_reqs))
        tmp_path = f.name

    try:
        result = subprocess.run(
            [uv_path, "pip", "compile", tmp_path, "--quiet"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        return {
            "tool": "uv",
            "available": True,
            "success": result.returncode == 0,
            "output": result.stdout[:2000] if result.stdout else "",
            "error": result.stderr[:2000] if result.returncode != 0 else "",
        }
    except subprocess.TimeoutExpired:
        return {
            "tool": "uv",
            "available": True,
            "success": False,
            "error": "Resolution timed out after 120 seconds",
        }
    except OSError as e:
        return {
            "tool": "uv",
            "available": True,
            "success": False,
            "error": str(e),
        }
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def try_pip_check() -> dict[str, Any]:
    """Run pip check to validate currently installed packages.

    Returns:
        Dict with check result: {tool, success, output, error}.
    """
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "check"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        return {
            "tool": "pip check",
            "available": True,
            "success": result.returncode == 0,
            "output": result.stdout[:2000] if result.stdout else "",
            "error": result.stderr[:2000] if result.returncode != 0 else "",
        }
    except subprocess.TimeoutExpired:
        return {
            "tool": "pip check",
            "available": True,
            "success": False,
            "error": "pip check timed out after 60 seconds",
        }
    except OSError as e:
        return {
            "tool": "pip check",
            "available": False,
            "error": str(e),
        }


def merge_requirements(
    existing_reqs: list[str], overrides: list[str]
) -> list[str]:
    """Merge existing requirements with overrides (overrides win).

    Args:
        existing_reqs: List of existing requirement strings.
        overrides: List of override requirement strings.

    Returns:
        Merged list of requirement strings.
    """
    # Index by normalized name, overrides replace existing
    by_name: dict[str, str] = {}
    for req in existing_reqs:
        name = parse_requirement_name(req)
        if name:
            by_name[name] = req

    for req in overrides:
        name = parse_requirement_name(req)
        if name:
            by_name[name] = req

    return list(by_name.values())


def resolve_dependencies(
    existing_reqs: list[str], overrides: list[str]
) -> dict[str, Any]:
    """Run the full dependency resolution check.

    Args:
        existing_reqs: List of existing requirement strings.
        overrides: List of dependency override strings from the PR.

    Returns:
        Complete dependency resolution report.
    """
    # Find direct conflicts
    conflicts = find_version_conflicts(existing_reqs, overrides)

    # Merge and try resolution
    merged = merge_requirements(existing_reqs, overrides)

    # Try uv first, then pip check as fallback
    resolution = try_uv_resolve(merged)
    if not resolution.get("available"):
        resolution = try_pip_check()

    resolved = resolution.get("success", False) if resolution.get("available") else None

    suggestions: list[str] = []
    for conflict in conflicts:
        suggestions.append(
            f"Package '{conflict['package']}': existing='{conflict['existing']}', "
            f"override='{conflict['override']}'. "
            f"Verify the override version is compatible with other dependencies."
        )

    return {
        "conflicts": conflicts,
        "resolved": resolved,
        "resolution_tool": resolution,
        "existing_count": len(existing_reqs),
        "override_count": len(overrides),
        "merged_count": len(merged),
        "suggestions": suggestions,
    }


def main() -> int:
    """Main entry point for the dependency resolver."""
    parser = argparse.ArgumentParser(
        description="Check for dependency conflicts when adding a new node pack",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--overrides",
        nargs="*",
        default=[],
        help="Dependency override strings (e.g., 'torch>=2.0' 'numpy<2.0')",
    )
    parser.add_argument(
        "--overrides-file",
        help="File containing dependency overrides (one per line)",
    )
    parser.add_argument(
        "--changes",
        help="Path to changes.json to extract dependency_overrides from",
    )
    parser.add_argument(
        "--requirements",
        default="requirements.txt",
        help="Path to existing requirements.txt (default: requirements.txt)",
    )
    parser.add_argument(
        "--output",
        help="Output file path for JSON report (default: stdout)",
    )
    args = parser.parse_args()

    # Collect overrides from all sources
    overrides: list[str] = list(args.overrides)

    if args.overrides_file:
        overrides_path = Path(args.overrides_file)
        if overrides_path.exists():
            overrides.extend(load_requirements(overrides_path))
        else:
            print(f"Warning: overrides file '{args.overrides_file}' not found", file=sys.stderr)

    if args.changes:
        changes_path = Path(args.changes)
        if changes_path.exists():
            overrides.extend(extract_overrides_from_changes(changes_path))
        else:
            print(f"Warning: changes file '{args.changes}' not found", file=sys.stderr)

    if not overrides:
        report = {
            "conflicts": [],
            "resolved": True,
            "existing_count": 0,
            "override_count": 0,
            "merged_count": 0,
            "suggestions": [],
            "note": "No dependency overrides provided; nothing to check.",
        }
    else:
        existing_reqs = load_requirements(Path(args.requirements))
        report = resolve_dependencies(existing_reqs, overrides)

    output = json.dumps(report, indent=2)

    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
        print(f"Report written to {args.output}", file=sys.stderr)
    else:
        print(output)

    # Exit 1 if unresolvable conflicts
    if report.get("resolved") is False and report.get("conflicts"):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
