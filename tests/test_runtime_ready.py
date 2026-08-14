"""Tests for strict runtime publication markers."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from runtime_manifest import canonical_json  # noqa: E402

SPEC = importlib.util.spec_from_file_location("runtime_ready", ROOT / "scripts/runtime_ready.py")
assert SPEC and SPEC.loader
runtime_ready = importlib.util.module_from_spec(SPEC)
sys.modules["runtime_ready"] = runtime_ready
SPEC.loader.exec_module(runtime_ready)


class RuntimeReadyTests(unittest.TestCase):
    def manifest(self) -> dict[str, object]:
        entries = [
            {
                "path": "opt/conda/bin/python",
                "type": "file",
                "mode": 0o755,
                "size_bytes": 0,
                "sha256": "e" * 64,
            }
        ]
        tree_sha256 = hashlib.sha256(canonical_json(entries)).hexdigest()
        return {
            "schema_version": 1,
            "runtime_version": "test-runtime",
            "runtime_digest": "sha256:" + tree_sha256,
            "source": {
                "image": "ghcr.io/example/runtime",
                "image_digest": "sha256:" + "b" * 64,
                "build_sha": "c" * 40,
            },
            "compatibility": {
                "platform": "linux/amd64",
                "launcher_digest": "sha256:" + "d" * 64,
                "launcher_abi": "comfy-pod-launcher/v1",
            },
            "entrypoint": {"path": "opt/conda/bin/python", "argv": ["opt/conda/bin/python"]},
            "targets": ["/app/comfyui", "/opt/conda"],
            "selection_policy": {
                "targets": ["/app/comfyui", "/opt/conda"],
                "include_app": [],
                "excludes": [],
            },
            "file_tree": {
                "entry_count": len(entries),
                "total_bytes": 0,
                "tree_sha256": tree_sha256,
                "entries": entries,
            },
            "archive": {
                "format": "tar.zst",
                "object_name": "sha256-" + "f" * 64 + ".tar.zst",
                "size_bytes": 1,
                "sha256": "f" * 64,
            },
        }

    def write_manifest(self, directory: Path, *, newline: bool = True) -> tuple[Path, bytes]:
        path = directory / "manifest.json"
        payload = json.dumps(self.manifest(), sort_keys=True, separators=(",", ":")).encode("utf-8")
        if newline:
            payload += b"\n"
        path.write_bytes(payload)
        return path, payload

    def test_marker_has_exact_shape_and_binds_exact_manifest_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path, manifest_bytes = self.write_manifest(root)
            ready_path = root / "READY.json"

            marker = runtime_ready.write_ready_marker(manifest_path, ready_path)

            self.assertEqual(set(marker), {"schema_version", "runtime_digest", "manifest_sha256"})
            self.assertEqual(marker["schema_version"], 1)
            self.assertEqual(marker["runtime_digest"], self.manifest()["runtime_digest"])
            self.assertEqual(marker["manifest_sha256"], hashlib.sha256(manifest_bytes).hexdigest())
            self.assertEqual(json.loads(ready_path.read_bytes()), marker)

    def test_marker_changes_when_manifest_bytes_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path, _ = self.write_manifest(root)
            ready_path = root / "READY.json"
            first = runtime_ready.write_ready_marker(manifest_path, ready_path)

            manifest_path.write_bytes(manifest_path.read_bytes() + b"\n")
            second = runtime_ready.write_ready_marker(manifest_path, ready_path)

            self.assertEqual(first["runtime_digest"], second["runtime_digest"])
            self.assertNotEqual(first["manifest_sha256"], second["manifest_sha256"])

    def test_invalid_manifest_does_not_replace_existing_marker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path, _ = self.write_manifest(root)
            ready_path = root / "READY.json"
            runtime_ready.write_ready_marker(manifest_path, ready_path)
            original = ready_path.read_bytes()

            manifest_path.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(runtime_ready.RuntimeReadyError, "manifest is invalid"):
                runtime_ready.write_ready_marker(manifest_path, ready_path)
            self.assertEqual(ready_path.read_bytes(), original)

    def test_rejects_extra_ready_fields_at_consumer_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path, manifest_bytes = self.write_manifest(root)
            marker = runtime_ready.build_ready_marker(manifest_bytes, self.manifest())
            marker["unexpected"] = True
            self.assertNotEqual(set(marker), {"schema_version", "runtime_digest", "manifest_sha256"})
            self.assertEqual(set(json.loads(json.dumps(marker))), {
                "schema_version",
                "runtime_digest",
                "manifest_sha256",
                "unexpected",
            })


if __name__ == "__main__":
    unittest.main()
