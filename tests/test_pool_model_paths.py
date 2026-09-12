"""Tests for the lease-free Pool worker shared-model path helper."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "pool_model_paths", ROOT / "docker" / "pod" / "pool_model_paths.py"
)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class PoolModelPathsTests(unittest.TestCase):
    def test_config_covers_every_pinned_comfyui_model_folder(self) -> None:
        config = module.pool_model_paths_config()
        self.assertEqual(set(config), {"comfy_pool_shared_models"})
        entry = config["comfy_pool_shared_models"]
        self.assertEqual(entry["base_path"], "/runpod-volume")
        self.assertEqual(
            set(entry) - {"base_path"},
            set(module.CANONICAL_MODEL_FOLDERS),
        )
        for folder in module.CANONICAL_MODEL_FOLDERS:
            self.assertEqual(entry[folder], f"models/{folder}/")

    def test_writer_is_atomic_and_uses_private_file_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "extra-model-paths.json"
            module.write_pool_extra_model_paths(
                config_path,
                mounted_volume_root=Path(directory),
            )

            self.assertEqual(
                json.loads(config_path.read_text(encoding="utf-8")),
                module.pool_model_paths_config(),
            )
            self.assertEqual(config_path.stat().st_mode & 0o777, 0o600)
            self.assertFalse(config_path.with_name(config_path.name + ".tmp").exists())

    def test_writer_rejects_relative_destination(self) -> None:
        with self.assertRaisesRegex(module.PoolModelPathsError, "absolute"):
            module.write_pool_extra_model_paths(Path("extra-model-paths.json"))

    def test_writer_fails_closed_without_network_volume_mount(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "not-mounted"
            config_path = Path(directory) / "extra-model-paths.json"
            with self.assertRaisesRegex(
                module.PoolModelPathsError,
                "Network Volume is not mounted",
            ):
                module.write_pool_extra_model_paths(
                    config_path,
                    mounted_volume_root=missing,
                )

    def test_config_has_no_lease_or_credential_inputs(self) -> None:
        source = (ROOT / "docker" / "pod" / "pool_model_paths.py").read_text(
            encoding="utf-8"
        )
        for forbidden in (
            "COMFY_CONTROL_PLANE_URL",
            "COMFY_INSTANCE_ID",
            "COMFY_POD_TOKEN",
            "X-Comfy-Pod-Token",
            "api/internal/pod-models",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
