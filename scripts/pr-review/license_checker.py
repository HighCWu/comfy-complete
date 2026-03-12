#!/usr/bin/env python3
"""License checker for custom node packs.

Checks node pack licenses and dependency licenses against a blocklist
of non-commercial or restrictive licenses.

Usage:
    python license_checker.py --path /tmp/nodes/my-node-pack
    python license_checker.py --path /tmp/nodes/my-node-pack --output report.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


# License files to search for (case-insensitive matching)
LICENSE_FILENAMES = [
    "LICENSE",
    "LICENSE.md",
    "LICENSE.txt",
    "LICENSE.rst",
    "LICENCE",
    "LICENCE.md",
    "LICENCE.txt",
    "COPYING",
    "COPYING.md",
    "COPYING.txt",
]

# Blocklisted license identifiers and patterns
BLOCKED_LICENSE_PATTERNS = [
    r"AGPL",
    r"GNU\s+Affero",
    r"GPL-3\.0",
    r"GPLv3",
    r"GNU\s+General\s+Public\s+License\s+v(ersion\s+)?3",
]

# Blocklisted packages/projects (non-commercial or problematic)
BLOCKED_PACKAGES = [
    "insightface",
    "deepface",
]

# Blocklisted license types from pip-licenses output
BLOCKED_LICENSE_TYPES = [
    "AGPL",
    "GNU Affero General Public License",
    "GPL-3.0",
    "GPLv3",
    "GNU General Public License v3",
]


def find_license_file(repo_path: Path) -> Path | None:
    """Find the license file in a repository directory.

    Args:
        repo_path: Path to the repository root.

    Returns:
        Path to the license file if found, None otherwise.
    """
    for name in LICENSE_FILENAMES:
        # Case-insensitive search
        for item in repo_path.iterdir():
            if item.is_file() and item.name.upper() == name.upper():
                return item
    return None


def read_license_content(license_path: Path) -> str:
    """Read and return the content of a license file.

    Args:
        license_path: Path to the license file.

    Returns:
        License file content as string.
    """
    try:
        return license_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def detect_license_type(content: str) -> str:
    """Detect the license type from file content.

    Args:
        content: License file content.

    Returns:
        Detected license type string.
    """
    content_upper = content.upper()

    if "MIT LICENSE" in content_upper or "PERMISSION IS HEREBY GRANTED, FREE OF CHARGE" in content_upper:
        return "MIT"
    if "APACHE LICENSE" in content_upper and "VERSION 2.0" in content_upper:
        return "Apache-2.0"
    if "BSD" in content_upper and ("2-CLAUSE" in content_upper or "SIMPLIFIED" in content_upper):
        return "BSD-2-Clause"
    if "BSD" in content_upper and ("3-CLAUSE" in content_upper or "NEW" in content_upper or "MODIFIED" in content_upper):
        return "BSD-3-Clause"
    if "GNU AFFERO GENERAL PUBLIC LICENSE" in content_upper:
        return "AGPL-3.0"
    if "GNU GENERAL PUBLIC LICENSE" in content_upper:
        if "VERSION 3" in content_upper:
            return "GPL-3.0"
        if "VERSION 2" in content_upper:
            return "GPL-2.0"
        return "GPL (unknown version)"
    if "GNU LESSER GENERAL PUBLIC LICENSE" in content_upper:
        return "LGPL"
    if "MOZILLA PUBLIC LICENSE" in content_upper:
        return "MPL-2.0"
    if "UNLICENSE" in content_upper or "THIS IS FREE AND UNENCUMBERED SOFTWARE" in content_upper:
        return "Unlicense"
    if "CREATIVE COMMONS" in content_upper:
        if "NONCOMMERCIAL" in content_upper or "NC" in content_upper:
            return "CC-NC (Non-Commercial)"
        return "Creative Commons"
    if "ISC LICENSE" in content_upper:
        return "ISC"
    if "DO WHAT THE FUCK YOU WANT" in content_upper:
        return "WTFPL"

    return "Unknown"


def is_license_blocked(license_type: str) -> bool:
    """Check if a license type is on the blocklist.

    Args:
        license_type: The detected license type string.

    Returns:
        True if the license is blocked.
    """
    for pattern in BLOCKED_LICENSE_PATTERNS:
        if re.search(pattern, license_type, re.IGNORECASE):
            return True
    if "non-commercial" in license_type.lower() or "NC" in license_type:
        return True
    return False


def check_node_license(repo_path: Path) -> dict[str, Any]:
    """Check the node pack's own license.

    Args:
        repo_path: Path to the node pack directory.

    Returns:
        Dict with license info: {file, type, blocked, content_snippet}.
    """
    license_file = find_license_file(repo_path)
    if license_file is None:
        return {
            "file": None,
            "type": "No license file found",
            "blocked": False,
            "warning": "No license file found in repository root",
        }

    content = read_license_content(license_file)
    license_type = detect_license_type(content)
    blocked = is_license_blocked(license_type)

    result: dict[str, Any] = {
        "file": str(license_file.name),
        "type": license_type,
        "blocked": blocked,
    }
    if blocked:
        result["reason"] = f"License type '{license_type}' is on the blocklist"

    return result


def parse_requirements_files(repo_path: Path) -> list[str]:
    """Parse dependency names from requirements files and pyproject.toml.

    Args:
        repo_path: Path to the node pack directory.

    Returns:
        List of dependency package names.
    """
    deps: list[str] = []

    # Check requirements.txt variants
    for req_file in ["requirements.txt", "requirements_base.txt", "install_requires.txt"]:
        req_path = repo_path / req_file
        if req_path.exists():
            try:
                for line in req_path.read_text(encoding="utf-8", errors="replace").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#") and not line.startswith("-"):
                        # Extract package name (before version specifier)
                        pkg_name = re.split(r"[>=<!\[\];]", line)[0].strip()
                        if pkg_name:
                            deps.append(pkg_name.lower())
            except OSError:
                pass

    # Check pyproject.toml
    pyproject_path = repo_path / "pyproject.toml"
    if pyproject_path.exists():
        try:
            content = pyproject_path.read_text(encoding="utf-8", errors="replace")
            # Simple regex to find dependencies in pyproject.toml
            in_deps = False
            for line in content.splitlines():
                if re.match(r"\s*dependencies\s*=\s*\[", line):
                    in_deps = True
                    continue
                if in_deps:
                    if "]" in line:
                        in_deps = False
                        continue
                    match = re.match(r'\s*["\']([^"\'>=<!\[;]+)', line)
                    if match:
                        deps.append(match.group(1).strip().lower())
        except OSError:
            pass

    # Check setup.py
    setup_path = repo_path / "setup.py"
    if setup_path.exists():
        try:
            content = setup_path.read_text(encoding="utf-8", errors="replace")
            # Find install_requires list
            match = re.search(r"install_requires\s*=\s*\[(.*?)\]", content, re.DOTALL)
            if match:
                for dep_match in re.finditer(r'["\']([^"\'>=<!\[;]+)', match.group(1)):
                    deps.append(dep_match.group(1).strip().lower())
        except OSError:
            pass

    return list(set(deps))


def check_pip_licenses() -> list[dict[str, str]]:
    """Run pip-licenses to get installed package licenses.

    Returns:
        List of dicts with package license info, or empty list if pip-licenses
        is not available.
    """
    try:
        result = subprocess.run(
            [sys.executable, "-m", "piplicenses", "--format=json", "--with-urls"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0:
            return json.loads(result.stdout)
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
        pass

    # Try alternative command
    try:
        result = subprocess.run(
            ["pip-licenses", "--format=json", "--with-urls"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0:
            return json.loads(result.stdout)
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
        pass

    return []


def check_dependency_licenses(repo_path: Path) -> dict[str, Any]:
    """Check licenses of pip dependencies.

    Args:
        repo_path: Path to the node pack directory.

    Returns:
        Dict with dependency license analysis results.
    """
    deps = parse_requirements_files(repo_path)
    blocked_deps: list[dict[str, str]] = []
    warnings: list[str] = []

    if not deps:
        return {
            "dependencies_found": 0,
            "blocked_deps": [],
            "warnings": ["No dependency files found (requirements.txt, pyproject.toml, setup.py)"],
        }

    # Check for blocklisted packages by name
    for dep in deps:
        dep_lower = dep.lower()
        for blocked_pkg in BLOCKED_PACKAGES:
            if blocked_pkg in dep_lower:
                blocked_deps.append({
                    "package": dep,
                    "reason": f"Package '{dep}' is on the blocklist (non-commercial or problematic license)",
                })

    # Try pip-licenses for installed packages
    pip_license_data = check_pip_licenses()
    if pip_license_data:
        pkg_licenses = {item.get("Name", "").lower(): item for item in pip_license_data}
        for dep in deps:
            dep_lower = dep.lower().replace("-", "_")
            # Try both dash and underscore variants
            license_info = pkg_licenses.get(dep_lower) or pkg_licenses.get(dep_lower.replace("_", "-"))
            if license_info:
                license_name = license_info.get("License", "Unknown")
                for blocked_type in BLOCKED_LICENSE_TYPES:
                    if blocked_type.lower() in license_name.lower():
                        blocked_deps.append({
                            "package": dep,
                            "license": license_name,
                            "reason": f"Dependency '{dep}' has blocked license: {license_name}",
                        })
                        break
    else:
        warnings.append("pip-licenses not available; dependency license check was skipped. Install with: pip install pip-licenses")

    return {
        "dependencies_found": len(deps),
        "dependencies": deps,
        "blocked_deps": blocked_deps,
        "warnings": warnings,
    }


def check_model_licenses(repo_path: Path) -> dict[str, Any]:
    """Check for model declarations and their potential license issues.

    Scans Python files for HuggingFace model references and checks for
    known problematic model licenses.

    Args:
        repo_path: Path to the node pack directory.

    Returns:
        Dict with model license analysis results.
    """
    blocked_models: list[dict[str, str]] = []
    warnings: list[str] = []
    model_refs: list[str] = []

    # Patterns that indicate model downloads from HuggingFace
    hf_patterns = [
        r'hf_hub_download\s*\(\s*["\']([^"\']+)["\']',
        r'snapshot_download\s*\(\s*["\']([^"\']+)["\']',
        r'from_pretrained\s*\(\s*["\']([^"\']+)["\']',
        r'repo_id\s*=\s*["\']([^"\']+)["\']',
    ]

    # Known model repos with non-commercial licenses
    blocked_model_patterns = [
        ("insightface", "InsightFace models have non-commercial license restrictions"),
        ("deepface", "DeepFace may include models with restrictive licenses"),
        ("buffalo_l", "InsightFace buffalo_l model has non-commercial license"),
        ("antelopev2", "InsightFace antelopev2 model has non-commercial license"),
    ]

    try:
        for py_file in repo_path.rglob("*.py"):
            try:
                content = py_file.read_text(encoding="utf-8", errors="replace")
                for pattern in hf_patterns:
                    for match in re.finditer(pattern, content):
                        model_ref = match.group(1)
                        if model_ref not in model_refs:
                            model_refs.append(model_ref)
            except OSError:
                continue
    except OSError:
        warnings.append("Could not scan Python files for model references")

    # Check model refs against blocked patterns
    for model_ref in model_refs:
        model_lower = model_ref.lower()
        for pattern, reason in blocked_model_patterns:
            if pattern in model_lower:
                blocked_models.append({
                    "model": model_ref,
                    "reason": reason,
                })
                break

    return {
        "model_references_found": len(model_refs),
        "model_references": model_refs,
        "blocked_models": blocked_models,
        "warnings": warnings,
    }


def run_license_check(repo_path: Path) -> dict[str, Any]:
    """Run the full license check on a node pack.

    Args:
        repo_path: Path to the node pack directory.

    Returns:
        Complete license check report as dict.
    """
    node_license = check_node_license(repo_path)
    dep_result = check_dependency_licenses(repo_path)
    model_result = check_model_licenses(repo_path)

    # Aggregate all warnings
    all_warnings: list[str] = []
    if node_license.get("warning"):
        all_warnings.append(node_license["warning"])
    all_warnings.extend(dep_result.get("warnings", []))
    all_warnings.extend(model_result.get("warnings", []))

    # Determine if any blocking issue was found
    has_blocker = (
        node_license.get("blocked", False)
        or len(dep_result.get("blocked_deps", [])) > 0
        or len(model_result.get("blocked_models", [])) > 0
    )

    return {
        "path": str(repo_path),
        "node_license": node_license,
        "blocked_deps": dep_result.get("blocked_deps", []),
        "blocked_models": model_result.get("blocked_models", []),
        "warnings": all_warnings,
        "has_blocker": has_blocker,
        "dependency_details": {
            "count": dep_result.get("dependencies_found", 0),
            "dependencies": dep_result.get("dependencies", []),
        },
        "model_details": {
            "count": model_result.get("model_references_found", 0),
            "references": model_result.get("model_references", []),
        },
    }


def main() -> int:
    """Main entry point for the license checker."""
    parser = argparse.ArgumentParser(
        description="Check licenses for a custom node pack",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--path",
        required=True,
        help="Path to the cloned node pack directory",
    )
    parser.add_argument(
        "--output",
        help="Output file path for JSON report (default: stdout)",
    )
    args = parser.parse_args()

    repo_path = Path(args.path)
    if not repo_path.is_dir():
        print(f"Error: '{args.path}' is not a valid directory", file=sys.stderr)
        return 1

    report = run_license_check(repo_path)
    output = json.dumps(report, indent=2)

    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
        print(f"Report written to {args.output}", file=sys.stderr)
    else:
        print(output)

    return 1 if report["has_blocker"] else 0


if __name__ == "__main__":
    sys.exit(main())
