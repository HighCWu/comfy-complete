#!/usr/bin/env python3
"""Registry checker for custom node packs.

Verifies that node packs exist on the Comfy Registry by checking the
public API endpoint.

Usage:
    python registry_checker.py --name comfyui-kjnodes
    python registry_checker.py --names comfyui-kjnodes comfyui-impact-pack
    python registry_checker.py --changes changes.json
    python registry_checker.py --changes changes.json --output report.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REGISTRY_API_BASE = "https://api.comfy.org/nodes"
REQUEST_TIMEOUT = 10


def _make_request(url: str, method: str = "HEAD") -> tuple[int, str | None]:
    """Make an HTTP request, trying requests library first, then urllib.

    Args:
        url: URL to request.
        method: HTTP method (HEAD or GET).

    Returns:
        Tuple of (status_code, error_string_or_None).
    """
    # Try requests library first
    try:
        import requests

        if method == "HEAD":
            resp = requests.head(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        else:
            resp = requests.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        return resp.status_code, None
    except ImportError:
        pass
    except Exception as e:
        return 0, str(e)

    # Fallback to urllib
    try:
        import urllib.request
        import urllib.error

        req = urllib.request.Request(url, method=method.upper())
        req.add_header("User-Agent", "comfy-complete-registry-checker/1.0")
        try:
            resp = urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT)
            return resp.getcode(), None
        except urllib.error.HTTPError as e:
            return e.code, None
        except urllib.error.URLError as e:
            return 0, str(e.reason)
    except Exception as e:
        return 0, str(e)


def check_registry(name: str) -> dict[str, Any]:
    """Check if a node pack exists on the Comfy Registry.

    Args:
        name: The node pack name to check.

    Returns:
        Dict with check result: {name, exists, status_code, error}.
    """
    # Skip URL-based node packs (GitHub direct references)
    if name.startswith("http://") or name.startswith("https://"):
        return {
            "name": name,
            "exists": None,
            "status_code": None,
            "error": None,
            "note": "URL-based node pack; registry check skipped",
        }

    url = f"{REGISTRY_API_BASE}/{name}"
    status_code, error = _make_request(url)

    return {
        "name": name,
        "exists": status_code == 200,
        "status_code": status_code,
        "error": error,
    }


def extract_names_from_changes(changes_path: Path) -> list[str]:
    """Extract node pack names from a changes.json file.

    Args:
        changes_path: Path to the changes.json file.

    Returns:
        List of node pack names from new and updated entries.
    """
    names: list[str] = []
    try:
        data = json.loads(changes_path.read_text(encoding="utf-8"))
        for node in data.get("new", []):
            name = node.get("name", "")
            if name:
                names.append(name)
        for node in data.get("updated", []):
            name = node.get("name", "")
            if name:
                names.append(name)
    except (OSError, json.JSONDecodeError, KeyError):
        pass
    return names


def run_registry_check(names: list[str]) -> dict[str, Any]:
    """Check multiple node packs against the registry.

    Args:
        names: List of node pack names to check.

    Returns:
        Complete registry check report.
    """
    results: list[dict[str, Any]] = []
    for name in names:
        result = check_registry(name)
        results.append(result)

    found = sum(1 for r in results if r.get("exists") is True)
    not_found = sum(1 for r in results if r.get("exists") is False)
    skipped = sum(1 for r in results if r.get("exists") is None)
    errors = sum(1 for r in results if r.get("error") is not None)

    return {
        "results": results,
        "summary": {
            "total": len(names),
            "found": found,
            "not_found": not_found,
            "skipped": skipped,
            "errors": errors,
        },
    }


def main() -> int:
    """Main entry point for the registry checker."""
    parser = argparse.ArgumentParser(
        description="Check if node packs exist on the Comfy Registry",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--name",
        help="Single node pack name to check",
    )
    parser.add_argument(
        "--names",
        nargs="*",
        default=[],
        help="Multiple node pack names to check",
    )
    parser.add_argument(
        "--changes",
        help="Path to changes.json to extract names from",
    )
    parser.add_argument(
        "--output",
        help="Output file path for JSON report (default: stdout)",
    )
    args = parser.parse_args()

    # Collect names from all sources
    names: list[str] = []
    if args.name:
        names.append(args.name)
    names.extend(args.names)

    if args.changes:
        changes_path = Path(args.changes)
        if changes_path.exists():
            names.extend(extract_names_from_changes(changes_path))
        else:
            print(f"Warning: changes file '{args.changes}' not found", file=sys.stderr)

    if not names:
        print("Error: No node pack names provided. Use --name, --names, or --changes.", file=sys.stderr)
        return 1

    # Deduplicate while preserving order
    seen: set[str] = set()
    unique_names: list[str] = []
    for name in names:
        if name not in seen:
            seen.add(name)
            unique_names.append(name)

    report = run_registry_check(unique_names)
    output = json.dumps(report, indent=2)

    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
        print(f"Report written to {args.output}", file=sys.stderr)
    else:
        print(output)

    return 0


if __name__ == "__main__":
    sys.exit(main())
