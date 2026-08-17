"""Focused tests for the fail-closed slim Pod runtime launcher."""

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

SPEC = importlib.util.spec_from_file_location("runtime_launcher", ROOT / "docker/pod/runtime_launcher.py")
assert SPEC and SPEC.loader
launcher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(launcher)

BOOTSTRAP_SPEC = importlib.util.spec_from_file_location(
    "pod_model_bootstrap",
    ROOT / "docker/pod/model_bootstrap.py",
)
assert BOOTSTRAP_SPEC and BOOTSTRAP_SPEC.loader
model_bootstrap = importlib.util.module_from_spec(BOOTSTRAP_SPEC)
BOOTSTRAP_SPEC.loader.exec_module(model_bootstrap)


class RuntimeLauncherTests(unittest.TestCase):
    def fixture(self, root: Path) -> tuple[Path, Path, dict[str, object]]:
        runtime = root / "generation"
        python = runtime / "opt/conda/bin/python"
        main = runtime / "app/comfyui/main.py"
        python.parent.mkdir(parents=True)
        main.parent.mkdir(parents=True)
        python.write_bytes(b"python")
        python.chmod(0o755)
        main.write_bytes(b"main")
        entries = []
        for directory in ("app/comfyui", "opt/conda", "opt/conda/bin"):
            path = runtime / directory
            entries.append({"path": directory, "type": "directory", "mode": path.stat().st_mode & 0o7777, "size_bytes": 0})
        for relative, path in (("app/comfyui/main.py", main), ("opt/conda/bin/python", python)):
            entries.append({
                "path": relative,
                "type": "file",
                "mode": path.stat().st_mode & 0o7777,
                "size_bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            })
        entries.sort(key=lambda item: item["path"])
        tree_digest = hashlib.sha256(canonical_json(entries)).hexdigest()
        archive_sha = "a" * 64
        manifest = {
            "schema_version": 1,
            "runtime_version": "test",
            "runtime_digest": f"sha256:{tree_digest}",
            "source": {"image": "example.invalid/runtime", "image_digest": f"sha256:{'b' * 64}", "build_sha": "c" * 40},
            "compatibility": {"platform": "linux/amd64", "launcher_digest": f"sha256:{'d' * 64}", "launcher_abi": "comfy-pod-launcher/v1"},
            "entrypoint": {"path": "opt/conda/bin/python", "argv": ["opt/conda/bin/python", "app/comfyui/main.py"]},
            "targets": ["/app/comfyui", "/opt/conda"],
            "selection_policy": {"targets": ["/app/comfyui", "/opt/conda"], "include_app": [], "excludes": [], "exclude_directory_names": [".git"]},
            "file_tree": {"entry_count": len(entries), "total_bytes": 10, "tree_sha256": tree_digest, "entries": entries},
            "archive": {"format": "tar.zst", "object_name": f"sha256-{archive_sha}.tar.zst", "size_bytes": 1, "sha256": archive_sha},
        }
        manifest_path = runtime / "manifest.json"
        manifest_path.write_bytes(canonical_json(manifest) + b"\n")
        ready_path = runtime / "READY.json"
        ready_path.write_text(json.dumps({
            "schema_version": 1,
            "runtime_digest": manifest["runtime_digest"],
            "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        }), encoding="utf-8")
        return manifest_path, ready_path, manifest

    def load(self, manifest_path: Path, ready_path: Path) -> dict[str, object]:
        return launcher.load_verified_manifest(
            manifest_path,
            ready_path,
            launcher_digest=f"sha256:{'d' * 64}",
            launcher_abi="comfy-pod-launcher/v1",
            platform="linux/amd64",
        )

    def test_verifies_materialized_runtime_and_installs_links(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest_path, ready_path, _ = self.fixture(root)
            manifest = self.load(manifest_path, ready_path)
            launcher.verify_runtime_tree(manifest_path.parent, manifest)
            link = root / "compat/conda"
            launcher.install_compatibility_link(link, manifest_path.parent / "opt/conda")
            self.assertTrue(link.is_symlink())

    def test_rejects_ready_marker_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest_path, ready_path, _ = self.fixture(Path(temporary))
            ready_path.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(launcher.LauncherError, "READY"):
                self.load(manifest_path, ready_path)

    def test_rejects_runtime_file_digest_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest_path, ready_path, _ = self.fixture(Path(temporary))
            manifest = self.load(manifest_path, ready_path)
            (manifest_path.parent / "app/comfyui/main.py").write_bytes(b"changed")
            with self.assertRaisesRegex(launcher.LauncherError, "digest"):
                launcher.verify_runtime_tree(manifest_path.parent, manifest)

    def test_full_audit_detects_noncritical_file_corruption(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest_path, ready_path, _ = self.fixture(Path(temporary))
            manifest = self.load(manifest_path, ready_path)
            python = manifest_path.parent / "opt/conda/bin/python"
            python.write_bytes(b"python")
            # main.py is the manifest entrypoint in this fixture, so add a new
            # noncritical file by treating python as noncritical for this check.
            manifest["entrypoint"]["path"] = "app/comfyui/main.py"
            python.write_bytes(b"broken")
            with self.assertRaisesRegex(launcher.LauncherError, "digest"):
                launcher.verify_runtime_tree(manifest_path.parent, manifest, full=True)

    def test_launch_check_does_not_walk_noncritical_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest_path, ready_path, _ = self.fixture(Path(temporary))
            manifest = self.load(manifest_path, ready_path)
            missing = manifest_path.parent / "app/comfyui/noncritical.bin"
            manifest["file_tree"]["entries"].append({
                "path": "app/comfyui/noncritical.bin",
                "type": "file",
                "mode": 0o644,
                "size_bytes": 7,
                "sha256": hashlib.sha256(b"missing").hexdigest(),
            })
            launcher.verify_runtime_tree(manifest_path.parent, manifest)
            with self.assertRaisesRegex(launcher.LauncherError, "missing"):
                launcher.verify_runtime_tree(manifest_path.parent, manifest, full=True)
            self.assertFalse(missing.exists())

    def test_launch_check_rejects_missing_critical_manifest_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest_path, ready_path, _ = self.fixture(Path(temporary))
            manifest = self.load(manifest_path, ready_path)
            manifest["file_tree"]["entries"] = [
                entry
                for entry in manifest["file_tree"]["entries"]
                if entry["path"] != "app/comfyui"
            ]
            with self.assertRaisesRegex(launcher.LauncherError, "launch-critical"):
                launcher.verify_runtime_tree(manifest_path.parent, manifest)

    def test_rejects_real_compatibility_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target"
            target.mkdir()
            link = root / "compat"
            link.mkdir()
            with self.assertRaisesRegex(launcher.LauncherError, "real compatibility"):
                launcher.install_compatibility_link(link, target)

    def test_comfy_projection_keeps_model_folders_local_but_source_writes_shared(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = root / "runtime"
            (runtime / "models/checkpoints").mkdir(parents=True)
            (runtime / "comfy").mkdir()
            (runtime / "comfy/__init__.py").write_text("", encoding="utf-8")
            (runtime / "models/checkpoints/bundled.safetensors").write_bytes(b"model")
            (runtime / "main.py").write_text("pass\n", encoding="utf-8")
            projected = root / "app/comfyui"
            launcher.install_comfy_projection(projected, runtime)
            self.assertTrue((projected / "main.py").is_symlink())
            self.assertTrue((projected / "models").is_dir())
            self.assertFalse((projected / "models").is_symlink())
            self.assertTrue((projected / "models/checkpoints").is_dir())
            self.assertFalse((projected / "models/checkpoints").is_symlink())
            self.assertTrue((projected / "models/checkpoints/bundled.safetensors").is_symlink())
            (projected / "models/checkpoints/instance.safetensors").symlink_to(root / "instance-model")
            self.assertFalse((runtime / "models/checkpoints/instance.safetensors").exists())
            (projected / "comfy/__pycache__").mkdir()
            self.assertTrue((runtime / "comfy/__pycache__").is_dir())

    def test_comfy_projection_is_idempotent_after_container_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = root / "runtime"
            (runtime / "models/checkpoints").mkdir(parents=True)
            (runtime / "main.py").write_text("pass\n", encoding="utf-8")
            projected = root / "app/comfyui"

            launcher.install_comfy_projection(projected, runtime)
            marker = projected.parent / ".comfyui.runtime-projection"
            self.assertEqual(marker.read_text(encoding="utf-8").strip(), str(runtime.resolve()))
            launcher.install_comfy_projection(projected, runtime)
            self.assertTrue((projected / "main.py").is_symlink())

    def test_comfy_projection_rejects_unmarked_existing_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = root / "runtime"
            runtime.mkdir()
            projected = root / "app/comfyui"
            projected.mkdir(parents=True)
            (projected / "user-data").write_text("do not replace\n", encoding="utf-8")

            with self.assertRaisesRegex(launcher.LauncherError, "existing ComfyUI path"):
                launcher.install_comfy_projection(projected, runtime)

    def test_multiple_projections_keep_instance_model_links_out_of_shared_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = root / "runtime"
            (runtime / "models/checkpoints").mkdir(parents=True)
            (runtime / "main.py").write_text("pass\n", encoding="utf-8")
            item = {"folder": "checkpoints", "filename": "same.safetensors"}

            resolved_targets = []
            for instance in ("one", "two"):
                projected = root / instance / "app/comfyui"
                launcher.install_comfy_projection(projected, runtime)
                instance_models = root / instance / "models"
                target = instance_models / "checkpoints/same.safetensors"
                target.parent.mkdir(parents=True)
                target.write_bytes(instance.encode("utf-8"))
                model_bootstrap.publish_comfy_model_links(
                    projected / "models",
                    instance_models,
                    [item],
                )
                resolved_targets.append(
                    (projected / "models/checkpoints/same.safetensors").resolve(strict=True)
                )

            self.assertNotEqual(resolved_targets[0], resolved_targets[1])
            self.assertFalse((runtime / "models/checkpoints/same.safetensors").exists())


if __name__ == "__main__":
    unittest.main()
