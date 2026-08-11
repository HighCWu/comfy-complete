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
    def test_rejects_unsafe_manifest_paths(self) -> None:
        with self.assertRaisesRegex(module.BootstrapError, "unsafe metadata"):
            module.validate_item(
                {
                    "folder": "checkpoints",
                    "filename": "../private.safetensors",
                    "size_bytes": 1,
                    "sha256": "a" * 64,
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
            "download_path": f"/api/internal/pod-models/artifacts/{sha256}?instance_id=inst_test",
        }
        with tempfile.TemporaryDirectory() as directory:
            model_root = Path(directory)
            target_dir = model_root / "upscale_models"
            target_dir.mkdir()
            partial = target_dir / "model.pth.partial"
            partial.write_bytes(payload[:7])
            starts: list[int | None] = []

            def fake_request(_url: str, _token: str, start: int | None = None) -> FakeResponse:
                starts.append(start)
                return FakeResponse(payload[start or 0 :], 206 if start else 200)

            with patch.object(module, "request", side_effect=fake_request):
                module.download_model("https://laimon.ai", "token", model_root, item)

            self.assertEqual(starts, [7])
            self.assertEqual((target_dir / "model.pth").read_bytes(), payload)
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
                    "download_path": "/api/internal/pod-models/artifacts/" + "b" * 64,
                }
            ],
        }
        env = {
            "LAIMON_POD_TOKEN": "token",
            "LAIMON_INSTANCE_ID": "inst_test",
            "LAIMON_CONTROL_PLANE_URL": "https://laimon.ai",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "instance"
            config = Path(directory) / "extra-model-paths.json"
            comfy_models = Path(directory) / "comfy-models"
            with patch.dict(os.environ, env, clear=False), patch.object(
                module, "load_manifest", return_value=manifest
            ), patch.object(module, "download_model") as download:
                with patch.object(module, "publish_comfy_model_links") as publish:
                    result = module.bootstrap(root, config, comfy_models)

            self.assertEqual(result, {"status": "ready", "item_count": 1, "total_bytes": 4})
            download.assert_called_once()
            publish.assert_called_once()
            self.assertEqual(
                json.loads(config.read_text(encoding="utf-8")),
                {
                    "laimon_instance": {
                        "base_path": str(root),
                        "loras": "models/loras/",
                    }
                },
            )

    def test_publishes_verified_models_into_comfy_default_folders(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            instance_models = root / "instance-models"
            target = instance_models / "upscale_models" / "model.pth"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"verified")
            comfy_models = root / "comfy-models"
            item = {"folder": "upscale_models", "filename": "model.pth"}

            module.publish_comfy_model_links(comfy_models, instance_models, [item])

            link = comfy_models / "upscale_models" / "model.pth"
            self.assertTrue(link.is_symlink())
            self.assertEqual(link.resolve(strict=True), target.resolve(strict=True))
            module.publish_comfy_model_links(comfy_models, instance_models, [item])

    def test_rejects_comfy_default_model_path_collisions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            instance_models = root / "instance-models"
            target = instance_models / "checkpoints" / "model.safetensors"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"verified")
            comfy_models = root / "comfy-models"
            collision = comfy_models / "checkpoints" / "model.safetensors"
            collision.parent.mkdir(parents=True)
            collision.write_bytes(b"base-image-model")

            with self.assertRaisesRegex(module.BootstrapError, "path collision"):
                module.publish_comfy_model_links(
                    comfy_models,
                    instance_models,
                    [{"folder": "checkpoints", "filename": "model.safetensors"}],
                )

    def test_rejects_broken_comfy_model_links(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            instance_models = root / "instance-models"
            target = instance_models / "vae" / "model.safetensors"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"verified")
            link = root / "comfy-models" / "vae" / "model.safetensors"
            link.parent.mkdir(parents=True)
            link.symlink_to(root / "missing-model.safetensors")

            with self.assertRaisesRegex(module.BootstrapError, "link collision"):
                module.publish_comfy_model_links(
                    root / "comfy-models",
                    instance_models,
                    [{"folder": "vae", "filename": "model.safetensors"}],
                )


if __name__ == "__main__":
    unittest.main()
