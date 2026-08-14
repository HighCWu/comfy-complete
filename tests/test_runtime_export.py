"""Focused tests for the local flattened runtime export prototype."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


manifest_module = load_script("runtime_manifest")
export_module = load_script("export_runtime")


class RuntimeExportTests(unittest.TestCase):
    def make_tree(self, root: Path) -> None:
        (root / "opt/conda/bin").mkdir(parents=True)
        (root / "app/comfyui/bin").mkdir(parents=True)
        (root / "app/comfyui/output").mkdir()
        (root / "app/comfyui/models/_xdgcache").mkdir(parents=True)
        (root / "opt/conda/bin/python").write_bytes(b"python-runtime")
        python = root / "opt/conda/bin/python"
        python.chmod(0o755)
        (root / "app/comfyui/bin/launcher").write_bytes(b"launcher")
        (root / "app/comfyui/bin/launcher").chmod(0o755)
        (root / "app/comfyui/README").write_bytes(b"immutable")
        (root / "app/comfyui/output/result.png").write_bytes(b"mutable")
        (root / "app/comfyui/models/_xdgcache/cache").write_bytes(b"mutable")
        os.symlink("../README", root / "app/comfyui/bin/readme-link")

    def export(self, root: Path, output: Path, *, entrypoint: str = "/app/comfyui/bin/launcher"):
        return export_module.export_runtime(
            source_root=root,
            output_dir=output,
            targets=["/opt/conda", "/app/comfyui"],
            app_files=[],
            exclusions=[
                "/app/comfyui/output",
                "/app/comfyui/models/_xdgcache",
            ],
            runtime_version="test-1",
            source_image="example/runtime-base",
            source_image_digest="sha256:" + "1" * 64,
            build_sha="2" * 40,
            platform="linux/amd64",
            launcher_digest="sha256:" + "3" * 64,
            launcher_abi="laimon-launcher/v1",
            python_version="3.11.15",
            cuda_version="12.8",
            glibc_version="2.39",
            entrypoint=entrypoint,
            entrypoint_args=["--port", "8188"],
        )

    def test_entrypoint_symlink_to_executable_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "source"
            self.make_tree(source)
            os.symlink("launcher", source / "app/comfyui/bin/launcher-link")
            _, manifest_path, manifest = self.export(
                source,
                base / "out",
                entrypoint="/app/comfyui/bin/launcher-link",
            )
            self.assertEqual(manifest["entrypoint"]["path"], "app/comfyui/bin/launcher-link")
            manifest_module.validate_manifest(
                json.loads(manifest_path.read_text(encoding="utf-8"))
            )

    def test_entrypoint_symlink_to_non_executable_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "source"
            self.make_tree(source)
            (source / "app/comfyui/bin/non-executable").write_bytes(b"not executable")
            os.symlink("non-executable", source / "app/comfyui/bin/launcher-link")
            _, _, manifest = self.export(source, base / "out-valid")
            manifest["entrypoint"] = {
                "path": "app/comfyui/bin/launcher-link",
                "argv": ["app/comfyui/bin/launcher-link", "--port", "8188"],
            }
            with self.assertRaisesRegex(manifest_module.RuntimeManifestError, "executable"):
                manifest_module.validate_manifest(manifest)
            with self.assertRaisesRegex(export_module.RuntimeExportError, "executable"):
                self.export(
                    source,
                    base / "out-invalid",
                    entrypoint="/app/comfyui/bin/launcher-link",
                )

    def test_runtime_identity_is_file_tree_not_archive_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "source"
            self.make_tree(source)
            archive, manifest_path, first = self.export(source, base / "out")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["archive"]["object_name"] = "sha256-" + "a" * 64 + ".tar.zst"
            manifest["archive"]["sha256"] = "a" * 64
            manifest["archive"]["size_bytes"] = 1
            manifest["runtime_digest"] = "sha256:" + manifest["file_tree"]["tree_sha256"]
            self.assertEqual(manifest["runtime_digest"], first["runtime_digest"])
            manifest_module.validate_manifest(manifest)

    def test_validator_accepts_equal_runtime_and_archive_digests(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "source"
            self.make_tree(source)
            _, manifest_path, _ = self.export(source, base / "out")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            runtime_hex = manifest["runtime_digest"].removeprefix("sha256:")
            manifest["archive"]["object_name"] = f"sha256-{runtime_hex}.tar.zst"
            manifest["archive"]["sha256"] = runtime_hex
            manifest_module.validate_manifest(manifest)

    def test_same_tree_with_different_archive_bytes_keeps_runtime_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "source"
            self.make_tree(source)
            archive_a, _, first = self.export(source, base / "out-a")
            entries = export_module.collect_entries(
                source,
                ["/opt/conda", "/app/comfyui"],
                [],
                ["/app/comfyui/output", "/app/comfyui/models/_xdgcache"],
            )
            archive_b = base / ("out-b/sha256-" + "b" * 64 + ".tar.zst")
            archive_b.parent.mkdir()
            archive_b.write_bytes(b"different-compression-frame")
            second = export_module._manifest(
                entries=entries,
                targets=["/opt/conda", "/app/comfyui"],
                app_files=[],
                exclusions=["/app/comfyui/output", "/app/comfyui/models/_xdgcache"],
                archive_path=archive_b,
                runtime_version="test-1",
                source_image="example/runtime-base",
                source_image_digest="sha256:" + "1" * 64,
                build_sha="2" * 40,
                platform="linux/amd64",
                launcher_digest="sha256:" + "3" * 64,
                launcher_abi="laimon-launcher/v1",
                python_version="3.11.15",
                cuda_version="12.8",
                glibc_version="2.39",
                entrypoint="/app/comfyui/bin/launcher",
                entrypoint_args=["--port", "8188"],
            )
            self.assertNotEqual(archive_a.read_bytes(), archive_b.read_bytes())
            self.assertEqual(first["runtime_digest"], second["runtime_digest"])

    def test_deterministic_export_has_stable_manifest_and_archive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "source"
            self.make_tree(source)
            first = base / "first"
            second = base / "second"
            archive_a, manifest_a, value_a = self.export(source, first)
            archive_b, manifest_b, value_b = self.export(source, second)
            self.assertEqual(archive_a.read_bytes(), archive_b.read_bytes())
            self.assertEqual(manifest_a.read_bytes(), manifest_b.read_bytes())
            self.assertEqual(value_a["runtime_digest"], value_b["runtime_digest"])
            self.assertEqual(value_a["archive"]["object_name"], archive_a.name)
            self.assertEqual(
                manifest_a.name,
                "sha256-"
                + value_a["runtime_digest"].removeprefix("sha256:")
                + "-"
                + value_a["archive"]["sha256"]
                + ".json",
            )
            self.assertEqual(
                value_a["file_tree"]["total_bytes"],
                len(b"python-runtime") + len(b"launcher") + len(b"immutable"),
            )
            self.assertTrue((first / archive_a.name).exists())
            export_module.verify_export(archive_a, manifest_a)

    def test_rejects_directory_outside_runtime_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "etc").mkdir()
            with self.assertRaisesRegex(export_module.RuntimeExportError, "not allowed"):
                export_module.collect_entries(root, ["/etc"], [], [])

    def test_rejects_traversal_and_escaping_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app/comfyui").mkdir(parents=True)
            (root / "app/comfyui/launcher").write_bytes(b"x")
            (root / "app/comfyui/launcher").chmod(0o755)
            os.symlink("../../../../secret", root / "app/comfyui/escape")
            with self.assertRaisesRegex(export_module.RuntimeExportError, "escapes runtime bundle"):
                export_module.collect_entries(root, ["/app/comfyui"], [], [])
            with self.assertRaisesRegex(export_module.RuntimeExportError, "absolute"):
                export_module._safe_absolute_source("/app/../etc", name="target")

    def test_rejects_symlink_to_unselected_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app/comfyui").mkdir(parents=True)
            (root / "app/comfyui/launcher").write_bytes(b"x")
            (root / "app/comfyui/launcher").chmod(0o755)
            os.symlink("../../outside", root / "app/comfyui/link")
            with self.assertRaisesRegex(export_module.RuntimeExportError, "not part of the selected"):
                export_module.collect_entries(root, ["/app/comfyui"], [], [])

    def test_accepts_file_symlink_through_a_symlinked_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            icu = root / "app/comfyui/lib/icu"
            (icu / "76.1").mkdir(parents=True)
            (icu / "76.1/Makefile.inc").write_text("icu\n", encoding="utf-8")
            os.symlink("76.1", icu / "current", target_is_directory=True)
            os.symlink("current/Makefile.inc", icu / "Makefile.inc")

            entries = export_module.collect_entries(root, ["/app/comfyui"], [], [])
            by_path = {entry.bundle_path: entry for entry in entries}
            resolved = export_module._resolve_symlink_chain(
                by_path["app/comfyui/lib/icu/Makefile.inc"],
                by_path,
            )
            self.assertEqual(resolved.bundle_path, "app/comfyui/lib/icu/76.1/Makefile.inc")
            self.assertEqual(resolved.kind, "file")

    def test_rejects_cycle_reached_through_a_symlinked_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            icu = root / "app/comfyui/lib/icu"
            icu.mkdir(parents=True)
            os.symlink("other", icu / "current", target_is_directory=True)
            os.symlink("current", icu / "other", target_is_directory=True)
            os.symlink("current/Makefile.inc", icu / "Makefile.inc")

            with self.assertRaisesRegex(export_module.RuntimeExportError, "symlink cycle"):
                export_module.collect_entries(root, ["/app/comfyui"], [], [])

    def test_rejects_selected_root_that_escapes_via_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "outside").mkdir()
            os.symlink("../outside", root / "app", target_is_directory=True)
            with self.assertRaisesRegex(export_module.RuntimeExportError, "escapes root"):
                export_module.collect_entries(root, ["/app/comfyui"], [], [])

    def test_manifest_rejects_corrupted_tree_digest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "source"
            self.make_tree(source)
            _, manifest_path, manifest = self.export(source, base / "out")
            corrupted = json.loads(manifest_path.read_text(encoding="utf-8"))
            file_entry = next(
                entry
                for entry in corrupted["file_tree"]["entries"]
                if entry["type"] == "file"
            )
            file_entry["sha256"] = "f" * 64
            with self.assertRaisesRegex(manifest_module.RuntimeManifestError, "tree_sha256"):
                manifest_module.validate_manifest(corrupted)

    def test_manifest_rejects_corrupted_archive_digest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "source"
            self.make_tree(source)
            archive, manifest_path, _ = self.export(source, base / "out")
            archive.write_bytes(archive.read_bytes() + b"corruption")
            with self.assertRaisesRegex(export_module.RuntimeExportError, "digest or size"):
                export_module.verify_export(archive, manifest_path)

    def test_selection_policy_records_and_rejects_excluded_entries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "source"
            self.make_tree(source)
            _, manifest_path, _ = self.export(source, base / "out")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["selection_policy"]["targets"], ["/app/comfyui", "/opt/conda"])
            self.assertEqual(manifest["selection_policy"]["include_app"], [])
            self.assertEqual(manifest["selection_policy"]["excludes"], ["/app/comfyui/models/_xdgcache", "/app/comfyui/output"])
            manifest["file_tree"]["entries"].append({
                "path": "app/comfyui/output/evil",
                "type": "file",
                "mode": 0o644,
                "size_bytes": 0,
                "sha256": "0" * 64,
            })
            manifest["file_tree"]["entries"].sort(key=lambda entry: entry["path"])
            manifest["file_tree"]["entry_count"] = len(manifest["file_tree"]["entries"])
            manifest["file_tree"]["total_bytes"] += 0
            manifest["file_tree"]["tree_sha256"] = manifest_module.sha256_bytes(
                manifest_module.canonical_json(manifest["file_tree"]["entries"])
            )
            with self.assertRaisesRegex(manifest_module.RuntimeManifestError, "excluded"):
                manifest_module.validate_manifest(manifest)

    def test_rejects_control_character_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "source"
            self.make_tree(source)
            _, manifest_path, _ = self.export(source, base / "out")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["runtime_version"] = "bad\nversion"
            with self.assertRaisesRegex(manifest_module.RuntimeManifestError, "control"):
                manifest_module.validate_manifest(manifest)

    def test_hard_links_are_exported_as_regular_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "source"
            self.make_tree(source)
            hard_link = source / "app/comfyui/bin/launcher-copy"
            os.link(source / "app/comfyui/bin/launcher", hard_link)
            _, manifest_path, _ = self.export(source, base / "out")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            kinds = {entry["path"]: entry["type"] for entry in manifest["file_tree"]["entries"]}
            self.assertEqual(kinds["app/comfyui/bin/launcher-copy"], "file")

    def test_missing_zstd_fails_clearly(self) -> None:
        with patch.object(export_module.shutil, "which", return_value=None):
            with self.assertRaisesRegex(export_module.RuntimeExportError, "zstd is required"):
                export_module._require_zstd()


if __name__ == "__main__":
    unittest.main()
