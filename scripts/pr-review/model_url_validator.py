#!/usr/bin/env python3
"""Model URL validator for supported_nodes.yaml.

Validates model URLs declared in supported_nodes.yaml by checking
accessibility via HEAD requests and reporting size information.

Usage:
    python model_url_validator.py --config supported_nodes.yaml
    python model_url_validator.py --url https://example.com/model.safetensors
    python model_url_validator.py --changes changes.json
    python model_url_validator.py --config supported_nodes.yaml --output report.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml

REQUEST_TIMEOUT = 10
SIZE_WARNING_BYTES = 10 * 1024 * 1024 * 1024  # 10 GB


def _head_request(url: str) -> tuple[int, int | None, str | None]:
    """Make a HEAD request to check URL accessibility.

    Args:
        url: URL to check.

    Returns:
        Tuple of (status_code, content_length_or_None, error_or_None).
    """
    # Try requests library first
    try:
        import requests

        resp = requests.head(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        content_length = resp.headers.get("Content-Length")
        size = int(content_length) if content_length else None
        return resp.status_code, size, None
    except ImportError:
        pass
    except Exception as e:
        return 0, None, str(e)

    # Fallback to urllib
    try:
        import urllib.request
        import urllib.error

        req = urllib.request.Request(url, method="HEAD")
        req.add_header("User-Agent", "comfy-complete-model-validator/1.0")
        try:
            resp = urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT)
            content_length = resp.headers.get("Content-Length")
            size = int(content_length) if content_length else None
            return resp.getcode(), size, None
        except urllib.error.HTTPError as e:
            return e.code, None, None
        except urllib.error.URLError as e:
            return 0, None, str(e.reason)
    except Exception as e:
        return 0, None, str(e)


def format_size(size_bytes: int) -> str:
    """Format bytes into a human-readable size string.

    Args:
        size_bytes: Size in bytes.

    Returns:
        Human-readable size string (e.g., '1.5 GB').
    """
    if size_bytes >= 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"
    if size_bytes >= 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.2f} MB"
    if size_bytes >= 1024:
        return f"{size_bytes / 1024:.2f} KB"
    return f"{size_bytes} B"


def validate_model_url(model: dict[str, str]) -> dict[str, Any]:
    """Validate a single model URL.

    Args:
        model: Dict with model info (name, url, directory, filename).

    Returns:
        Validation result dict.
    """
    url = model.get("url", "")
    name = model.get("name", model.get("filename", "unknown"))

    if not url:
        return {
            "name": name,
            "url": "",
            "accessible": False,
            "size_bytes": None,
            "size_human": None,
            "error": "No URL provided",
        }

    status_code, size_bytes, error = _head_request(url)
    accessible = 200 <= status_code < 400

    result: dict[str, Any] = {
        "name": name,
        "url": url,
        "accessible": accessible,
        "status_code": status_code,
        "size_bytes": size_bytes,
        "size_human": format_size(size_bytes) if size_bytes else None,
        "error": error,
    }

    if size_bytes and size_bytes > SIZE_WARNING_BYTES:
        result["warning"] = f"Model is larger than 10 GB ({format_size(size_bytes)})"

    return result


def extract_models_from_config(config_path: Path) -> list[dict[str, str]]:
    """Extract model entries from supported_nodes.yaml.

    Args:
        config_path: Path to supported_nodes.yaml.

    Returns:
        List of model dicts with name, url, directory, filename.
    """
    models: list[dict[str, str]] = []
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        if not data:
            return models

        for node_pack in data.get("node_packs", []):
            pack_name = node_pack.get("name", "unknown")
            for model in node_pack.get("models", []):
                model_entry = dict(model)
                model_entry.setdefault("source_pack", pack_name)
                models.append(model_entry)
    except (OSError, yaml.YAMLError):
        pass

    return models


def extract_models_from_changes(changes_path: Path) -> list[dict[str, str]]:
    """Extract model entries from a changes.json file.

    Args:
        changes_path: Path to changes.json.

    Returns:
        List of model dicts.
    """
    models: list[dict[str, str]] = []
    try:
        data = json.loads(changes_path.read_text(encoding="utf-8"))
        for node in data.get("new", []):
            for model in node.get("models", []):
                model_entry = dict(model)
                model_entry.setdefault("source_pack", node.get("name", "unknown"))
                models.append(model_entry)
        for node in data.get("updated", []):
            head = node.get("head", {})
            for model in head.get("models", []):
                model_entry = dict(model)
                model_entry.setdefault("source_pack", node.get("name", "unknown"))
                models.append(model_entry)
    except (OSError, json.JSONDecodeError, KeyError):
        pass

    return models


def run_model_validation(models: list[dict[str, str]]) -> dict[str, Any]:
    """Validate all model URLs.

    Args:
        models: List of model dicts to validate.

    Returns:
        Complete validation report.
    """
    results: list[dict[str, Any]] = []
    for model in models:
        result = validate_model_url(model)
        results.append(result)

    accessible_count = sum(1 for r in results if r.get("accessible"))
    inaccessible_count = sum(1 for r in results if not r.get("accessible"))
    warnings = [r for r in results if r.get("warning")]

    return {
        "models": results,
        "summary": {
            "total": len(models),
            "accessible": accessible_count,
            "inaccessible": inaccessible_count,
            "large_model_warnings": len(warnings),
        },
    }


def main() -> int:
    """Main entry point for the model URL validator."""
    parser = argparse.ArgumentParser(
        description="Validate model URLs declared in supported_nodes.yaml",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--config",
        help="Path to supported_nodes.yaml to extract model URLs from",
    )
    parser.add_argument(
        "--changes",
        help="Path to changes.json to extract model URLs from",
    )
    parser.add_argument(
        "--url",
        action="append",
        default=[],
        help="Individual model URL to validate (can be specified multiple times)",
    )
    parser.add_argument(
        "--output",
        help="Output file path for JSON report (default: stdout)",
    )
    args = parser.parse_args()

    models: list[dict[str, str]] = []

    # Collect models from all sources
    if args.config:
        config_path = Path(args.config)
        if config_path.exists():
            models.extend(extract_models_from_config(config_path))
        else:
            print(f"Warning: config file '{args.config}' not found", file=sys.stderr)

    if args.changes:
        changes_path = Path(args.changes)
        if changes_path.exists():
            models.extend(extract_models_from_changes(changes_path))
        else:
            print(f"Warning: changes file '{args.changes}' not found", file=sys.stderr)

    for url in args.url:
        models.append({"url": url, "name": url.split("/")[-1] if "/" in url else url})

    if not models:
        report: dict[str, Any] = {
            "models": [],
            "summary": {
                "total": 0,
                "accessible": 0,
                "inaccessible": 0,
                "large_model_warnings": 0,
            },
            "note": "No models found to validate.",
        }
    else:
        report = run_model_validation(models)

    output = json.dumps(report, indent=2)

    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
        print(f"Report written to {args.output}", file=sys.stderr)
    else:
        print(output)

    return 0


if __name__ == "__main__":
    sys.exit(main())
