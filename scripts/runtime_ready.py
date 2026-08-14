#!/usr/bin/env python3
"""Create the strict publication marker for a verified runtime manifest.

``manifest.json`` is the immutable runtime contract.  ``READY.json`` is a
small, separately published marker used by the Pod launcher to distinguish a
fully materialized runtime from a partial or stale copy.  This helper never
copies runtime bytes and never changes the manifest; it only validates the
manifest and atomically writes the marker which binds to the manifest's exact
bytes and declared runtime identity.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Sequence

from runtime_manifest import RuntimeManifestError, canonical_json, validate_manifest


READY_SCHEMA_VERSION = 1


class RuntimeReadyError(RuntimeError):
    """Raised when a READY marker cannot be created safely."""


def _read_verified_manifest(path: Path) -> tuple[bytes, dict[str, object]]:
    try:
        manifest_bytes = path.read_bytes()
        manifest = validate_manifest(json.loads(manifest_bytes))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RuntimeManifestError) as error:
        raise RuntimeReadyError(f"runtime manifest is invalid: {error}") from error
    return manifest_bytes, manifest


def build_ready_marker(manifest_bytes: bytes, manifest: dict[str, object]) -> dict[str, object]:
    """Return the exact, deliberately minimal READY marker for *manifest*."""

    # Revalidate at the boundary so callers cannot accidentally bind a marker
    # to an unchecked dictionary assembled by another tool.
    validated = validate_manifest(manifest)
    try:
        parsed_bytes = validate_manifest(json.loads(manifest_bytes))
    except (UnicodeDecodeError, json.JSONDecodeError, RuntimeManifestError) as error:
        raise RuntimeReadyError(f"runtime manifest bytes are invalid: {error}") from error
    if parsed_bytes != validated:
        raise RuntimeReadyError("runtime manifest bytes do not match the supplied manifest")
    return {
        "schema_version": READY_SCHEMA_VERSION,
        "runtime_digest": validated["runtime_digest"],
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
    }


def write_ready_marker(manifest_path: Path, ready_path: Path) -> dict[str, object]:
    """Validate *manifest_path* and atomically publish *ready_path*.

    The marker contains no archive URL, credentials, or mutable path.  Its
    exact three-key shape is intentional: consumers compare it as a complete
    object and reject additions as well as omissions.
    """

    manifest_bytes, manifest = _read_verified_manifest(manifest_path)
    marker = build_ready_marker(manifest_bytes, manifest)
    payload = canonical_json(marker) + b"\n"
    ready_path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=ready_path.parent,
            prefix=f".{ready_path.name}.",
            suffix=".partial",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, ready_path)
        temporary = None
    except OSError as error:
        raise RuntimeReadyError(f"could not publish READY marker: {error}") from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return marker


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True, help="validated runtime manifest")
    parser.add_argument("--output", type=Path, required=True, help="READY.json destination")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        marker = write_ready_marker(args.manifest, args.output)
    except (RuntimeReadyError, OSError) as error:
        print(f"runtime READY generation failed: {error}", file=os.sys.stderr)
        return 2
    print(json.dumps(marker, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
