#!/usr/bin/env python3
"""Compile and resolve dependency overrides from supported_nodes.yaml.

Uses static analysis (packaging + version comparison) by default, and
optionally delegates to `uv pip compile` for full transitive resolution.

Reuses parsing logic from scripts/extract_deps.py where possible.

Examples:
    # Static analysis of a pack's dependency_overrides against requirements.txt
    python scripts/add-node/compile-deps.py \\
        --yaml supported_nodes.yaml \\
        --name comfyui-example \\
        --requirements requirements.txt

    # Full resolution with uv (if available)
    python scripts/add-node/compile-deps.py \\
        --yaml supported_nodes.yaml \\
        --name comfyui-example \\
        --requirements requirements.txt \\
        --resolve

    # Machine-readable JSON output
    python scripts/add-node/compile-deps.py \\
        --yaml supported_nodes.yaml \\
        --name comfyui-example \\
        --requirements requirements.txt \\
        --json
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    print("Error: pyyaml is required. Install with: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

try:
    from packaging.requirements import Requirement
    from packaging.version import Version
    from packaging.specifiers import SpecifierSet
except ImportError:
    print(
        "Error: packaging is required. Install with: pip install packaging",
        file=sys.stderr,
    )
    sys.exit(1)

# Ensure stdout can handle unicode (needed on Windows with cp1252)
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Packages managed by the environment, never by node packs.
BLACKLISTED_PACKAGES = frozenset({
    "torch",
    "torchaudio",
    "torchsde",
    "torchvision",
})

# Packages that may never be downgraded by a dependency override.
PROTECTED_PACKAGES = frozenset({
    "torch",
    "transformers",
    "safetensors",
    "kornia",
})

# Known-good remaps: declared name -> canonical replacement.
PIP_OVERRIDES: dict[str, str] = {
    "opencv-python": "opencv-contrib-python-headless",
    "opencv-python-headless": "opencv-contrib-python-headless",
    "opencv-contrib-python": "opencv-contrib-python-headless",
}


# ---------------------------------------------------------------------------
# Shared parsing helpers (mirrors extract_deps.py)
# ---------------------------------------------------------------------------

def normalize_package_name(name: str) -> str:
    """PEP 503 normalization: lowercase, collapse [-_.] to hyphens."""
    return re.sub(r"[-_.]+", "-", name).lower()


@dataclass
class ParsedRequirement:
    """A parsed pip requirement line."""

    name: str
    version_spec: str  # e.g. "==1.2.3", ">=1.0,<2", ""
    full_line: str
    normalized_name: str = ""

    def __post_init__(self) -> None:
        self.normalized_name = normalize_package_name(self.name)


def parse_requirement(line: str) -> ParsedRequirement | None:
    """Parse a single pip requirement line. Returns None for comments/URLs."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith("git+") or line.startswith("http"):
        return None
    if " @ " in line:
        return None

    match = re.match(r"^([A-Za-z0-9][\w.\-]*)\s*(.*)", line)
    if not match:
        return None

    name = match.group(1)
    version_spec = match.group(2).strip()
    return ParsedRequirement(name=name, version_spec=version_spec, full_line=line)


def load_requirements(path: Path) -> dict[str, ParsedRequirement]:
    """Load requirements.txt into a dict keyed by normalized package name."""
    if not path.exists():
        return {}
    reqs: dict[str, ParsedRequirement] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        parsed = parse_requirement(line)
        if parsed:
            reqs[parsed.normalized_name] = parsed
    return reqs


def load_pack_overrides(yaml_path: Path, pack_name: str) -> list[str]:
    """Return the dependency_overrides list for a single named pack."""
    with open(yaml_path, encoding="utf-8") as f:
        data: dict[str, Any] = yaml.safe_load(f) or {}

    for pack in data.get("node_packs", []):
        if pack.get("name") == pack_name:
            overrides = pack.get("dependency_overrides", [])
            return [dep.strip() for dep in overrides if isinstance(dep, str)]

    return []


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------

@dataclass
class CheckResult:
    """Aggregated results of the dependency compilation check."""

    # Blacklist
    blacklisted: list[str] = field(default_factory=list)

    # Override analysis
    new_packages: list[str] = field(default_factory=list)
    compatible: list[str] = field(default_factory=list)
    conflicts: list[dict[str, str]] = field(default_factory=list)

    # Downgrade protection
    downgrades: list[dict[str, str]] = field(default_factory=list)

    # pip overrides applied
    remaps: list[dict[str, str]] = field(default_factory=list)

    # uv resolution
    uv_ran: bool = False
    uv_passed: bool | None = None
    uv_output: str = ""

    @property
    def has_blockers(self) -> bool:
        return bool(self.blacklisted) or bool(self.downgrades)

    @property
    def summary_line(self) -> str:
        parts = []
        parts.append(f"{len(self.conflicts)} conflict(s)")
        parts.append(f"{len(self.new_packages)} new package(s)")
        blockers = len(self.blacklisted) + len(self.downgrades)
        if not self.uv_passed and self.uv_ran:
            blockers += 1
        parts.append(f"{blockers} blocker(s)")
        return ", ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "pass": not self.has_blockers and (self.uv_passed is not False),
            "blacklisted": self.blacklisted,
            "new_packages": self.new_packages,
            "compatible": self.compatible,
            "conflicts": self.conflicts,
            "downgrades": self.downgrades,
            "remaps": self.remaps,
            "uv_ran": self.uv_ran,
            "uv_passed": self.uv_passed,
            "uv_output": self.uv_output,
            "summary": self.summary_line,
        }


# ---------------------------------------------------------------------------
# Core checks
# ---------------------------------------------------------------------------

def check_blacklist(overrides: list[str]) -> list[str]:
    """Return any blacklisted package names found in overrides."""
    found: list[str] = []
    for dep in overrides:
        parsed = parse_requirement(dep)
        if parsed and parsed.normalized_name in {normalize_package_name(b) for b in BLACKLISTED_PACKAGES}:
            found.append(dep)
    return found


def apply_pip_overrides(overrides: list[str]) -> tuple[list[str], list[dict[str, str]]]:
    """Remap known problem packages. Returns (remapped_overrides, remaps_applied)."""
    remapped: list[str] = []
    remaps: list[dict[str, str]] = []

    for dep in overrides:
        parsed = parse_requirement(dep)
        if parsed and parsed.normalized_name in {normalize_package_name(k) for k in PIP_OVERRIDES}:
            # Find the original key that matched
            for orig_name, replacement in PIP_OVERRIDES.items():
                if normalize_package_name(orig_name) == parsed.normalized_name:
                    new_dep = replacement + parsed.version_spec
                    remapped.append(new_dep)
                    remaps.append({"from": dep, "to": new_dep})
                    break
        else:
            remapped.append(dep)

    return remapped, remaps


def analyze_overrides(
    overrides: list[str],
    existing: dict[str, ParsedRequirement],
) -> tuple[list[str], list[str], list[dict[str, str]]]:
    """Compare overrides against existing requirements.

    Returns (new_packages, compatible, conflicts).
    """
    new_packages: list[str] = []
    compatible: list[str] = []
    conflicts: list[dict[str, str]] = []

    for dep in overrides:
        parsed = parse_requirement(dep)
        if not parsed:
            continue

        if parsed.normalized_name not in existing:
            new_packages.append(dep)
            continue

        existing_req = existing[parsed.normalized_name]

        if not parsed.version_spec:
            # No version constraint from override, always compatible
            compatible.append(f"{parsed.name} (no version constraint)")
            continue

        if not existing_req.version_spec:
            compatible.append(f"{parsed.name} (existing has no pin)")
            continue

        # Compare version specifiers
        if parsed.version_spec == existing_req.version_spec:
            compatible.append(f"{parsed.name}{parsed.version_spec} (matches existing pin)")
            continue

        # Check if the existing pinned version satisfies the override's specifier
        existing_pin_match = re.match(r"==\s*(.+)", existing_req.version_spec)
        if existing_pin_match:
            pinned_version = existing_pin_match.group(1).strip()
            try:
                spec = SpecifierSet(parsed.version_spec)
                if Version(pinned_version) in spec:
                    compatible.append(
                        f"{parsed.name}{parsed.version_spec} "
                        f"(existing {existing_req.full_line} satisfies)"
                    )
                    continue
            except Exception:
                pass

        conflicts.append({
            "package": parsed.name,
            "override": dep,
            "existing": existing_req.full_line,
        })

    return new_packages, compatible, conflicts


def check_downgrades(
    overrides: list[str],
    existing: dict[str, ParsedRequirement],
) -> list[dict[str, str]]:
    """Check if any protected package would be downgraded."""
    downgrades: list[dict[str, str]] = []
    protected_normalized = {normalize_package_name(p) for p in PROTECTED_PACKAGES}

    for dep in overrides:
        parsed = parse_requirement(dep)
        if not parsed:
            continue
        if parsed.normalized_name not in protected_normalized:
            continue
        if parsed.normalized_name not in existing:
            continue

        existing_req = existing[parsed.normalized_name]
        existing_pin_match = re.match(r"==\s*(.+)", existing_req.version_spec)
        if not existing_pin_match:
            continue

        pinned_version = existing_pin_match.group(1).strip()

        # Check for an upper bound that would exclude the current pin
        try:
            override_spec = SpecifierSet(parsed.version_spec)
            if Version(pinned_version) not in override_spec:
                downgrades.append({
                    "package": parsed.name,
                    "override": dep,
                    "existing": existing_req.full_line,
                })
        except Exception:
            # If we can't parse, be conservative and flag it
            downgrades.append({
                "package": parsed.name,
                "override": dep,
                "existing": existing_req.full_line,
                "note": "Could not parse version specifier for comparison",
            })

    return downgrades


# ---------------------------------------------------------------------------
# uv pip compile integration
# ---------------------------------------------------------------------------

def run_uv_resolve(
    overrides: list[str],
    requirements_path: Path,
) -> tuple[bool, str]:
    """Run `uv pip compile` with overrides + constraints.

    Returns (passed, output_text).
    """
    uv_bin = shutil.which("uv")
    if not uv_bin:
        return True, "uv not found; skipping full resolution."

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        # Write override requirements to a temp file
        override_file = tmp_path / "overrides.txt"
        override_file.write_text("\n".join(overrides) + "\n", encoding="utf-8")

        # Output file
        output_file = tmp_path / "resolved.txt"

        cmd = [
            uv_bin,
            "pip",
            "compile",
            str(override_file),
            "--constraint",
            str(requirements_path),
            "--output-file",
            str(output_file),
            "--quiet",
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            return False, "uv pip compile timed out after 120 seconds."
        except FileNotFoundError:
            return True, "uv binary not found; skipping full resolution."

        if result.returncode != 0:
            stderr = result.stderr.strip()
            return False, stderr or "uv pip compile failed with no output."

        resolved = ""
        if output_file.exists():
            resolved = output_file.read_text(encoding="utf-8").strip()

        return True, resolved or "All dependencies resolve without conflicts."


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------

def compile_deps(
    yaml_path: Path,
    pack_name: str,
    requirements_path: Path,
    do_resolve: bool = False,
) -> CheckResult:
    """Run all dependency checks for a single node pack."""
    result = CheckResult()

    overrides = load_pack_overrides(yaml_path, pack_name)
    if not overrides:
        return result

    existing = load_requirements(requirements_path)

    # 1. Blacklist check
    result.blacklisted = check_blacklist(overrides)

    # 2. pip overrides (remap problem packages)
    overrides, result.remaps = apply_pip_overrides(overrides)

    # 3. Override analysis (new / compatible / conflicts)
    result.new_packages, result.compatible, result.conflicts = analyze_overrides(
        overrides, existing
    )

    # 4. Downgrade protection
    result.downgrades = check_downgrades(overrides, existing)

    # 5. uv resolution (optional)
    if do_resolve and requirements_path.exists():
        result.uv_ran = True
        result.uv_passed, result.uv_output = run_uv_resolve(overrides, requirements_path)

    return result


def format_text(result: CheckResult, pack_name: str) -> str:
    """Format check results as human-readable text."""
    lines: list[str] = []
    lines.append("Dependency Check Results")
    lines.append("=" * 24)
    lines.append(f"Pack: {pack_name}")
    lines.append("")

    # Blacklist
    lines.append("Blacklist Check:")
    if result.blacklisted:
        for pkg in result.blacklisted:
            lines.append(f"  BLOCK {pkg}")
        lines.append(
            "        These packages are managed by the environment "
            "and must not appear in dependency_overrides."
        )
    else:
        lines.append("  PASS  No blacklisted packages in dependency_overrides")
    lines.append("")

    # Override analysis
    lines.append("Override Analysis:")
    if not result.new_packages and not result.compatible and not result.conflicts:
        lines.append("  (no dependency_overrides to analyze)")
    for pkg in result.new_packages:
        lines.append(f"  NEW   {pkg} (not in requirements.txt)")
    for pkg in result.compatible:
        lines.append(f"  OK    {pkg}")
    for c in result.conflicts:
        lines.append(
            f"  CONFLICT  {c['override']} (requirements.txt has {c['existing']})"
        )
    lines.append("")

    # Downgrade protection
    lines.append("Downgrade Protection:")
    if result.downgrades:
        for d in result.downgrades:
            lines.append(
                f"  BLOCK {d['override']} would restrict protected package "
                f"(existing: {d['existing']})"
            )
    else:
        lines.append("  PASS  No protected packages would be downgraded")
    lines.append("")

    # pip overrides
    if result.remaps:
        lines.append("pip Overrides Applied:")
        for r in result.remaps:
            lines.append(f"  REMAP {r['from']} -> {r['to']}")
        lines.append("")

    # uv resolution
    if result.uv_ran:
        lines.append("Resolution (uv pip compile):")
        if result.uv_passed:
            lines.append(f"  PASS  {result.uv_output.splitlines()[0] if result.uv_output else 'OK'}")
        else:
            lines.append("  FAIL  Resolution failed:")
            for uv_line in result.uv_output.splitlines():
                lines.append(f"        {uv_line}")
        lines.append("")

    lines.append(f"Summary: {result.summary_line}")
    return "\n".join(lines)


def main() -> int:
    """Entry point."""
    parser = argparse.ArgumentParser(
        description="Compile and check dependency overrides from supported_nodes.yaml.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python scripts/add-node/compile-deps.py \\\n"
            "      --yaml supported_nodes.yaml --name comfyui-example \\\n"
            "      --requirements requirements.txt\n"
            "\n"
            "  python scripts/add-node/compile-deps.py \\\n"
            "      --yaml supported_nodes.yaml --name comfyui-example \\\n"
            "      --requirements requirements.txt --resolve\n"
            "\n"
            "  python scripts/add-node/compile-deps.py \\\n"
            "      --yaml supported_nodes.yaml --name comfyui-example \\\n"
            "      --requirements requirements.txt --json\n"
        ),
    )
    parser.add_argument(
        "--yaml",
        type=Path,
        required=True,
        help="Path to supported_nodes.yaml",
    )
    parser.add_argument(
        "--name",
        required=True,
        help="Node pack name to check",
    )
    parser.add_argument(
        "--requirements",
        type=Path,
        required=True,
        help="Path to requirements.txt (base constraints)",
    )
    parser.add_argument(
        "--resolve",
        action="store_true",
        help="Run full resolution via uv pip compile (requires uv)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output results as JSON",
    )
    args = parser.parse_args()

    if not args.yaml.exists():
        msg = f"File not found: {args.yaml}"
        if args.json_output:
            print(json.dumps({"pass": False, "errors": [msg]}, indent=2))
        else:
            print(f"FAIL: {msg}", file=sys.stderr)
        return 1

    # Verify the pack exists
    with open(args.yaml, encoding="utf-8") as f:
        data: dict[str, Any] = yaml.safe_load(f) or {}
    packs = data.get("node_packs", [])
    pack_found = any(p.get("name") == args.name for p in packs)
    if not pack_found:
        msg = (
            f"Pack '{args.name}' not found in node_packs list. "
            f"Make sure the name matches exactly."
        )
        if args.json_output:
            print(json.dumps({"pass": False, "errors": [msg]}, indent=2))
        else:
            print(f"FAIL: {msg}", file=sys.stderr)
        return 1

    result = compile_deps(
        yaml_path=args.yaml,
        pack_name=args.name,
        requirements_path=args.requirements,
        do_resolve=args.resolve,
    )

    if args.json_output:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        print(format_text(result, args.name))

    # Exit 1 on blockers or uv failure
    if result.has_blockers or result.uv_passed is False:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
