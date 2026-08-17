"""Unit tests for the Pod's lease-bound model bootstrap client."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "pod_model_bootstrap", ROOT / "docker" / "pod" / "model_bootstrap.py"
)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class FakeResponse(io.BytesIO):
    def __init__(self, payload: bytes, status: int) -> None:
        super().__init__(payload)
        self.status = status

    def getcode(self) -> int:
        return self.status

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


class PodModelBootstrapTests(unittest.TestCase):
    def test_model_bootstrap_integration_is_optional(self) -> None:
        with patch.dict(module.os.environ, {}, clear=True):
            self.assertFalse(module.integration_configured())

    def test_model_bootstrap_rejects_partial_configuration(self) -> None:
        with patch.dict(module.os.environ, {"COMFY_INSTANCE_ID": "inst_test"}, clear=True):
            with self.assertRaisesRegex(module.BootstrapError, "configured together"):
                module.integration_configured()

    def test_rejects_unsafe_manifest_paths(self) -> None:
        with self.assertRaisesRegex(module.BootstrapError, "unsafe metadata"):
            module.validate_item(
                {
                    "folder": "checkpoints",
                    "filename": "../private.safetensors",
                    "size_bytes": 1,
                    "sha256": "a" * 64,
                    "source": "r2",
                    "download_path": "/api/internal/pod-models/artifacts/" + "a" * 64,
                }
            )

    def test_resumes_and_atomically_verifies_a_model_download(self) -> None:
        payload = b"verified-model-payload"
        sha256 = module.hashlib.sha256(payload).hexdigest()
        item = {
            "folder": "upscale_models",
            "filename": "model.pth",
            "size_bytes": len(payload),
            "sha256": sha256,
            "source": "r2",
            "download_path": f"/api/internal/pod-models/artifacts/{sha256}?instance_id=inst_test",
        }
        with tempfile.TemporaryDirectory() as directory:
            model_root = Path(directory)
            object_root = model_root / "objects"
            target = object_root / sha256[:2] / sha256 / "artifact"
            target.parent.mkdir(parents=True)
            partial = target.with_name(target.name + ".partial")
            partial.write_bytes(payload[:7])
            starts: list[int | None] = []

            def fake_request(_url: str, _token: str, start: int | None = None) -> FakeResponse:
                starts.append(start)
                return FakeResponse(payload[start or 0 :], 206 if start else 200)

            with patch.object(module, "request", side_effect=fake_request):
                module.download_model(
                    "https://example.invalid",
                    "token",
                    model_root / "models",
                    item,
                    object_root,
                )

            self.assertEqual(starts, [7])
            self.assertEqual(target.read_bytes(), payload)
            alias = model_root / "models" / "upscale_models" / "model.pth"
            self.assertTrue(alias.is_symlink())
            self.assertEqual(alias.resolve(strict=True), target.resolve(strict=True))
            self.assertFalse(partial.exists())

    def test_bootstrap_writes_only_instance_scoped_model_paths(self) -> None:
        manifest = {
            "version": 1,
            "instance_id": "inst_test",
            "status": "ready",
            "item_count": 1,
            "total_bytes": 4,
            "items": [
                {
                    "folder": "loras",
                    "filename": "model.safetensors",
                    "size_bytes": 4,
                    "sha256": "b" * 64,
                    "source": "r2",
                    "download_path": "/api/internal/pod-models/artifacts/" + "b" * 64,
                }
            ],
        }
        env = {
            "COMFY_POD_TOKEN": "token",
            "COMFY_INSTANCE_ID": "inst_test",
            "COMFY_CONTROL_PLANE_URL": "https://example.invalid",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "instance"
            config = Path(directory) / "extra-model-paths.json"
            with patch.dict(os.environ, env, clear=False), patch.object(
                module, "load_manifest", return_value=manifest
            ), patch.object(module, "download_model") as download:
                result = module.bootstrap(
                    root,
                    config,
                    Path(directory) / "shared-volume",
                )

            self.assertEqual(result, {"status": "ready", "item_count": 1, "total_bytes": 4})
            download.assert_called_once()
            self.assertEqual(
                json.loads(config.read_text(encoding="utf-8")),
                {
                    "comfy_instance": {
                        "base_path": str(root),
                        "loras": "models/loras/",
                    }
                },
            )

    def test_links_a_published_shared_model_without_downloading(self) -> None:
        payload = b"shared-model"
        sha256 = module.hashlib.sha256(payload).hexdigest()
        relative = "models/checkpoints/shared.safetensors"
        manifest = {
            "version": 1,
            "instance_id": "inst_test",
            "status": "ready",
            "item_count": 1,
            "total_bytes": len(payload),
            "items": [{
                "folder": "checkpoints",
                "filename": "shared.safetensors",
                "size_bytes": len(payload),
                "sha256": sha256,
                "source": "shared_volume",
                "shared_volume_path": relative,
                "shared_marker_path": relative + ".manifest.json",
            }],
        }
        env = {
            "COMFY_POD_TOKEN": "token",
            "COMFY_INSTANCE_ID": "inst_test",
            "COMFY_CONTROL_PLANE_URL": "https://example.invalid",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "runtime"
            shared = Path(directory) / "network-volume"
            artifact = shared / relative
            artifact.parent.mkdir(parents=True)
            artifact.write_bytes(payload)
            marker = shared / (relative + ".manifest.json")
            marker.write_text(json.dumps({
                "version": 1,
                "sha256": sha256,
                "size_bytes": len(payload),
                "artifact_path": relative,
            }), encoding="utf-8")
            config = Path(directory) / "extra-model-paths.json"
            with patch.dict(os.environ, env, clear=False), patch.object(
                module, "load_manifest", return_value=manifest
            ), patch.object(module, "download_model") as download:
                module.bootstrap(root, config, shared)

            download.assert_not_called()
            link = root / "models" / "checkpoints" / "shared.safetensors"
            self.assertTrue(link.is_symlink())
            self.assertEqual(link.resolve(strict=True), artifact.resolve(strict=True))

if __name__ == "__main__":
    unittest.main()
