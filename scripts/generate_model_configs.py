#!/usr/bin/env python3
"""Generate cloud-compatible model configuration from supported_nodes.yaml.

Extracts `models:` entries from node pack definitions and produces a JSON file
compatible with the cloud's supported_models.json format.

Usage:
    python scripts/generate_model_configs.py --yaml supported_nodes.yaml --output models.json
    python scripts/generate_model_configs.py --yaml supported_nodes.yaml  # stdout
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml


def load_supported_nodes(yaml_path: Path) -> dict[str, Any]:
    """Load and parse the supported_nodes.yaml file."""
    with open(yaml_path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def extract_models(supported_nodes: dict[str, Any]) -> tuple[list[dict[str, str]], list[str]]:
    """Extract all model entries from node packs.

    Returns a tuple of (models, warnings) where models is the deduplicated list
    and warnings contains any conflict/duplicate messages.
    """
    raw_models: list[tuple[str, dict[str, str]]] = []
    warnings: list[str] = []

    for pack in supported_nodes.get("node_packs", []):
        pack_name = pack.get("name", "unknown")
        models = pack.get("models", [])

        for model in models:
            if not isinstance(model, dict):
                warnings.append(f"Pack '{pack_name}': skipping non-dict model entry: {model}")
                continue

            name = model.get("name", "")
            if not name:
                warnings.append(f"Pack '{pack_name}': skipping model entry without 'name'")
                continue

            raw_models.append((pack_name, model))

    # Deduplicate by (directory, resolved_filename)
    seen: dict[tuple[str, str], tuple[str, dict[str, str]]] = {}
    deduplicated: list[dict[str, str]] = []

    for pack_name, model in raw_models:
        directory = model.get("directory", "")
        # Use explicit filename if provided, otherwise derive from name
        filename = model.get("filename", model["name"])
        dedup_key = (directory, filename)

        if dedup_key in seen:
            existing_pack, existing_model = seen[dedup_key]
            # Check for conflicts (same key but different URLs)
            if model.get("url", "") != existing_model.get("url", ""):
                warnings.append(
                    f"CONFLICT: Model '{filename}' in directory '{directory}' "
                    f"declared by both '{existing_pack}' and '{pack_name}' "
                    f"with different URLs:\n"
                    f"  {existing_pack}: {existing_model.get('url', '<no url>')}\n"
                    f"  {pack_name}: {model.get('url', '<no url>')}"
                )
            else:
                warnings.append(
                    f"Duplicate: Model '{filename}' in directory '{directory}' "
                    f"declared by both '{existing_pack}' and '{pack_name}' (same URL, deduped)"
                )
            continue

        seen[dedup_key] = (pack_name, model)

        # Build output entry in supported_models.json format
        output_entry: dict[str, str] = {
            "model_name": filename,
        }
        if model.get("url"):
            output_entry["url"] = model["url"]
        if directory:
            output_entry["directory"] = directory

        deduplicated.append(output_entry)

    return deduplicated, warnings


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Generate cloud model configs from supported_nodes.yaml",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python scripts/generate_model_configs.py --yaml supported_nodes.yaml\n"
            "  python scripts/generate_model_configs.py --yaml supported_nodes.yaml --output models.json\n"
        ),
    )
    parser.add_argument(
        "--yaml",
        type=Path,
        required=True,
        help="Path to supported_nodes.yaml",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output file path (default: stdout)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress warnings to stderr",
    )
    args = parser.parse_args()

    if not args.yaml.exists():
        print(f"Error: File not found: {args.yaml}", file=sys.stderr)
        return 1

    supported_nodes = load_supported_nodes(args.yaml)
    models, warnings = extract_models(supported_nodes)

    # Print warnings to stderr
    if warnings and not args.quiet:
        for warning in warnings:
            print(f"WARNING: {warning}", file=sys.stderr)
        print(file=sys.stderr)

    output = json.dumps({"models": models}, indent=2)

    if args.output:
        args.output.write_text(output + "\n", encoding="utf-8")
        print(
            f"Wrote {len(models)} models to {args.output}",
            file=sys.stderr,
        )
    else:
        print(output)

    return 0


if __name__ == "__main__":
    sys.exit(main())
