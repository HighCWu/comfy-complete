#!/usr/bin/env python3
"""Report and bound the compressed size of one immutable OCI/Docker image."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any


GIB = 1024**3


class ImageSizeAuditError(RuntimeError):
    """Raised when an image manifest is invalid or exceeds its guard."""


def load_remote_manifest(image: str) -> dict[str, Any]:
    result = subprocess.run(
        ["docker", "buildx", "imagetools", "inspect", "--raw", image],
        check=True,
        capture_output=True,
        text=True,
    )
    value = json.loads(result.stdout)
    if not isinstance(value, dict):
        raise ImageSizeAuditError("image manifest is not an object")
    return value


def inspect_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    layers = manifest.get("layers")
    if not isinstance(layers, list) or not layers:
        raise ImageSizeAuditError(
            "expected a single-platform image manifest with non-empty layers"
        )

    measured: list[dict[str, Any]] = []
    for index, layer in enumerate(layers):
        if not isinstance(layer, dict):
            raise ImageSizeAuditError(f"layer {index} is not an object")
        size = layer.get("size")
        digest = layer.get("digest")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ImageSizeAuditError(f"layer {index} has an invalid compressed size")
        if not isinstance(digest, str) or not digest.startswith("sha256:"):
            raise ImageSizeAuditError(f"layer {index} has an invalid digest")
        measured.append({"index": index, "sizeBytes": size, "digest": digest})

    ordered = sorted(measured, key=lambda item: item["sizeBytes"], reverse=True)
    total = sum(item["sizeBytes"] for item in measured)
    return {
        "schemaVersion": 1,
        "layerCount": len(measured),
        "compressedBytes": total,
        "compressedGiB": round(total / GIB, 3),
        "largestLayerBytes": ordered[0]["sizeBytes"],
        "largestLayerGiB": round(ordered[0]["sizeBytes"] / GIB, 3),
        "layersAtLeastOneGiB": sum(
            1 for item in measured if item["sizeBytes"] >= GIB
        ),
        "largestLayers": ordered[:10],
    }


def positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--image")
    source.add_argument("--manifest-file", type=Path)
    parser.add_argument("--max-compressed-gib", type=positive_float, required=True)
    parser.add_argument("--max-layer-gib", type=positive_float, required=True)
    args = parser.parse_args()

    if args.image:
        manifest = load_remote_manifest(args.image)
    else:
        manifest = json.loads(args.manifest_file.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise ImageSizeAuditError("image manifest is not an object")

    report = inspect_manifest(manifest)
    report["image"] = args.image
    report["limits"] = {
        "maxCompressedGiB": args.max_compressed_gib,
        "maxLayerGiB": args.max_layer_gib,
    }
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))

    errors: list[str] = []
    if report["compressedBytes"] > args.max_compressed_gib * GIB:
        errors.append(
            f"compressed image is {report['compressedGiB']} GiB, "
            f"above {args.max_compressed_gib:g} GiB"
        )
    if report["largestLayerBytes"] > args.max_layer_gib * GIB:
        errors.append(
            f"largest layer is {report['largestLayerGiB']} GiB, "
            f"above {args.max_layer_gib:g} GiB"
        )
    if errors:
        raise ImageSizeAuditError("; ".join(errors))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
