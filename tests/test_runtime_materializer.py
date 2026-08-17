"""Tests for the provider-neutral local runtime volume materializer."""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from runtime_manifest import canonical_json  # noqa: E402

SPEC = importlib.util.spec_from_file_location(
    "materialize_runtime",
    ROOT / "scripts" / "materialize_runtime.py",
)
assert SPEC and SPEC.loader
materializer = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = materializer
SPEC.loader.exec_module(materializer)


@unittest.skipUnless(shutil.which("zstd"), "zstd CLI is required")
class RuntimeMaterializerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.volume = self.root / "volume"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _entries(payload: bytes = b"runtime") -> list[dict[str, object]]:
        return [
            {
                "path": "app/comfyui",
                "type": "directory",
                "mode": 0o755,
                "size_bytes": 0,
            },
            {
                "path": "app/comfyui/main.py",
                "type": "file",
                "mode": 0o755,
                "size_bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            },
        ]

    def _archive(
        self,
        members: list[dict[str, object]],
        *,
        name_prefix: str = "archive",
    ) -> Path:
        tar_path = self.root / f"{name_prefix}.tar"
        with tarfile.open(tar_path, mode="w", format=tarfile.PAX_FORMAT) as archive:
            for member in members:
                info = tarfile.TarInfo(str(member["path"]))
                info.uid = 0
                info.gid = 0
                info.mtime = 0
                info.mode = int(member.get("mode", 0o755))
                info.pax_headers = {}
                kind = member["type"]
                if kind == "directory":
                    info.type = tarfile.DIRTYPE
                    info.size = 0
                    archive.addfile(info)
                elif kind == "file":
                    payload = bytes(member.get("payload", b""))
                    info.type = tarfile.REGTYPE
                    info.size = len(payload)
                    archive.addfile(info, io.BytesIO(payload))
                elif kind == "symlink":
                    info.type = tarfile.SYMTYPE
                    info.size = 0
                    info.linkname = str(member["link_target"])
                    archive.addfile(info)
                elif kind == "hardlink":
                    info.type = tarfile.LNKTYPE
                    info.size = 0
                    info.linkname = str(member.get("link_target", "app/comfyui/main.py"))
                    archive.addfile(info)
                elif kind == "fifo":
                    info.type = tarfile.FIFOTYPE
                    info.size = 0
                    archive.addfile(info)
                else:
                    raise AssertionError(f"unsupported test member: {kind}")

        output = self.root / f"{name_prefix}.tar.zst"
        with output.open("wb") as handle:
            completed = subprocess.run(
                ["zstd", "-q", "-T1", "--no-progress", "-c", str(tar_path)],
                stdout=handle,
                stderr=subprocess.PIPE,
                check=False,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8", "replace"))
        archive_sha = hashlib.sha256(output.read_bytes()).hexdigest()
        content_addressed = self.root / f"sha256-{archive_sha}.tar.zst"
        output.replace(content_addressed)
        return content_addressed

    def _manifest(
        self,
        entries: list[dict[str, object]],
        archive: Path,
        *,
        runtime_version: str = "test-runtime",
    ) -> Path:
        tree_sha = hashlib.sha256(canonical_json(entries)).hexdigest()
        manifest = {
            "schema_version": 1,
            "runtime_version": runtime_version,
            "runtime_digest": f"sha256:{tree_sha}",
            "source": {
                "image": "example/runtime",
                "image_digest": f"sha256:{'b' * 64}",
                "build_sha": "c" * 40,
            },
            "compatibility": {
                "platform": "linux/amd64",
                "launcher_digest": f"sha256:{'d' * 64}",
                "launcher_abi": "comfy-launcher/v1",
            },
            "entrypoint": {
                "path": "app/comfyui/main.py",
                "argv": ["app/comfyui/main.py"],
            },
            "targets": ["/app/comfyui"],
            "selection_policy": {
                "targets": ["/app/comfyui"],
                "include_app": [],
                "excludes": [],
                "exclude_directory_names": [".git"],
            },
            "file_tree": {
                "entry_count": len(entries),
                "total_bytes": sum(
                    int(entry["size_bytes"])
                    for entry in entries
                    if entry["type"] == "file"
                ),
                "tree_sha256": tree_sha,
                "entries": entries,
            },
            "archive": {
                "format": "tar.zst",
                "object_name": archive.name,
                "size_bytes": archive.stat().st_size,
                "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            },
        }
        path = self.root / f"manifest-{runtime_version}.json"
        path.write_bytes(canonical_json(manifest) + b"\n")
        return path

    def _valid_inputs(self, payload: bytes = b"runtime") -> tuple[Path, Path, list[dict[str, object]]]:
        entries = self._entries(payload)
        archive = self._archive(
            [
                {"path": "app/comfyui", "type": "directory", "mode": 0o755},
                {
                    "path": "app/comfyui/main.py",
                    "type": "file",
                    "mode": 0o755,
                    "payload": payload,
                },
            ]
        )
        return archive, self._manifest(entries, archive), entries

    def test_materializes_and_atomically_points_current(self) -> None:
        archive, manifest, entries = self._valid_inputs()

        result = materializer.materialize_runtime(archive, manifest, self.volume)

        self.assertEqual(result["status"], "materialized")
        self.assertTrue(result["current_updated"])
        runtime_hex = str(result["runtime_digest"])[len("sha256:") :]
        generation = self.volume / "runtimes" / runtime_hex
        self.assertEqual(str((self.volume / "runtimes" / "current").readlink()), runtime_hex)
        self.assertEqual((generation / "manifest.json").read_bytes(), manifest.read_bytes())
        ready = json.loads((generation / "READY.json").read_text(encoding="utf-8"))
        self.assertEqual(ready["runtime_digest"], result["runtime_digest"])
        self.assertEqual(len(entries), result["entry_count"])
        self.assertEqual((generation / "app/comfyui/main.py").read_bytes(), b"runtime")

    def test_published_control_paths_are_uid_neutral_but_staging_stays_private(self) -> None:
        archive, manifest, _ = self._valid_inputs()

        result = materializer.materialize_runtime(archive, manifest, self.volume)

        runtime_root = self.volume / "runtimes"
        generation = runtime_root / str(result["runtime_digest"])[len("sha256:") :]
        implicit_parent = generation / "app"
        metadata_paths = (generation / "manifest.json", generation / "READY.json")

        # These are materializer-owned control paths.  Other UIDs can traverse
        # and read them, while group/other users cannot write them.  The
        # manifest-owned app/comfyui and payload modes remain byte-for-byte
        # faithful to the source runtime.
        self.assertEqual(stat.S_IMODE(runtime_root.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE((runtime_root / ".staging").stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(generation.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(implicit_parent.stat().st_mode), 0o755)
        for path in metadata_paths:
            mode = stat.S_IMODE(path.stat().st_mode)
            self.assertEqual(mode, 0o644)
            self.assertTrue(mode & 0o004)
            self.assertEqual(mode & 0o002, 0)
        self.assertEqual(stat.S_IMODE((generation / "app/comfyui").stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE((generation / "app/comfyui/main.py").stat().st_mode), 0o755)

    def test_interrupted_seal_is_repaired_before_current_is_exposed(self) -> None:
        archive, manifest, _ = self._valid_inputs()
        first = materializer.materialize_runtime(archive, manifest, self.volume)
        generation = self.volume / "runtimes" / str(first["runtime_digest"])[len("sha256:") :]

        # Simulate a process dying after the generation rename but before its
        # materializer-owned directories were sealed.  The generation remains
        # valid, but the retry must repair its access contract before reusing it.
        (self.volume / "runtimes" / "current").unlink()
        generation.chmod(0o700)
        (generation / "app").chmod(0o700)
        second = materializer.materialize_runtime(archive, manifest, self.volume)

        self.assertEqual(second["status"], "reused")
        self.assertTrue((self.volume / "runtimes" / "current").is_symlink())
        self.assertEqual(stat.S_IMODE(generation.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE((generation / "app").stat().st_mode), 0o755)

    def test_reuse_tolerates_runtime_created_pycache(self) -> None:
        archive, manifest, _ = self._valid_inputs()
        first = materializer.materialize_runtime(archive, manifest, self.volume)
        generation = self.volume / "runtimes" / str(first["runtime_digest"])[len("sha256:") :]
        pycache = generation / "app/comfyui/__pycache__"
        pycache.mkdir()
        (pycache / "main.cpython-312.pyc").write_bytes(b"trusted-cache")

        second = materializer.materialize_runtime(archive, manifest, self.volume)

        self.assertEqual(second["status"], "reused")
        self.assertTrue((pycache / "main.cpython-312.pyc").is_file())

    def test_rejects_a_nontraversable_volume_root(self) -> None:
        self.volume.mkdir(mode=0o700)
        archive, manifest, _ = self._valid_inputs()

        with self.assertRaisesRegex(materializer.RuntimeMaterializerError, "volume_root_permissions"):
            materializer.materialize_runtime(archive, manifest, self.volume)

    def test_accepts_a_provider_permissive_volume_root(self) -> None:
        self.volume.mkdir(mode=0o777)
        self.volume.chmod(0o777)
        archive, manifest, _ = self._valid_inputs()

        result = materializer.materialize_runtime(archive, manifest, self.volume)

        self.assertEqual(result["status"], "materialized")

    def test_same_digest_is_reused_only_after_complete_verification(self) -> None:
        archive, manifest, _ = self._valid_inputs()
        first = materializer.materialize_runtime(archive, manifest, self.volume)
        second = materializer.materialize_runtime(archive, manifest, self.volume)
        self.assertEqual(second["status"], "reused")
        self.assertFalse(second["current_updated"])
        self.assertEqual(first["runtime_digest"], second["runtime_digest"])

        runtime_hex = str(first["runtime_digest"])[len("sha256:") :]
        generation = self.volume / "runtimes" / runtime_hex
        (generation / "app/comfyui/main.py").write_bytes(b"corrupt")
        current_target = (self.volume / "runtimes" / "current").readlink()
        with self.assertRaisesRegex(materializer.RuntimeMaterializerError, "materialized_digest_mismatch"):
            materializer.materialize_runtime(archive, manifest, self.volume)
        self.assertEqual((self.volume / "runtimes" / "current").readlink(), current_target)

    def test_extra_member_is_rejected_and_current_is_left_untouched(self) -> None:
        good_archive, good_manifest, _ = self._valid_inputs()
        good_result = materializer.materialize_runtime(good_archive, good_manifest, self.volume)
        current_target = (self.volume / "runtimes" / "current").readlink()

        entries = self._entries(b"new")
        archive = self._archive(
            [
                {"path": "app/comfyui", "type": "directory", "mode": 0o755},
                {"path": "app/comfyui/main.py", "type": "file", "mode": 0o755, "payload": b"new"},
                {"path": "app/comfyui/extra", "type": "file", "mode": 0o644, "payload": b"extra"},
            ],
            name_prefix="extra",
        )
        manifest = self._manifest(entries, archive, runtime_version="new-extra")
        with self.assertRaisesRegex(materializer.RuntimeMaterializerError, "unexpected_member"):
            materializer.materialize_runtime(archive, manifest, self.volume)
        self.assertEqual((self.volume / "runtimes" / "current").readlink(), current_target)
        self.assertEqual(good_result["status"], "materialized")
        staging = self.volume / "runtimes" / ".staging"
        self.assertEqual(list(staging.iterdir()), [])

    def test_missing_member_is_rejected(self) -> None:
        entries = self._entries()
        archive = self._archive(
            [{"path": "app/comfyui", "type": "directory", "mode": 0o755}],
            name_prefix="missing",
        )
        manifest = self._manifest(entries, archive, runtime_version="missing")
        with self.assertRaisesRegex(materializer.RuntimeMaterializerError, "archive_entries_missing"):
            materializer.materialize_runtime(archive, manifest, self.volume)

    def test_symlink_is_preserved_but_broken_target_fails_closed(self) -> None:
        payload = b"runtime"
        entries = self._entries(payload)
        entries.append(
            {
                "path": "app/comfyui/link",
                "type": "symlink",
                "mode": 0o777,
                "size_bytes": 0,
                "link_target": "main.py",
            }
        )
        entries.sort(key=lambda entry: str(entry["path"]))
        archive = self._archive(
            [
                {"path": "app/comfyui", "type": "directory", "mode": 0o755},
                {"path": "app/comfyui/main.py", "type": "file", "mode": 0o755, "payload": payload},
                {
                    "path": "app/comfyui/link",
                    "type": "symlink",
                    "mode": 0o777,
                    "link_target": "main.py",
                },
            ],
            name_prefix="symlink",
        )
        manifest = self._manifest(entries, archive, runtime_version="symlink")
        result = materializer.materialize_runtime(archive, manifest, self.volume)
        runtime_hex = str(result["runtime_digest"])[len("sha256:") :]
        link = self.volume / "runtimes" / runtime_hex / "app/comfyui/link"
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.readlink(), Path("main.py"))

        broken_entries = self._entries(payload)
        broken_entries.append(
            {
                "path": "app/comfyui/broken",
                "type": "symlink",
                "mode": 0o777,
                "size_bytes": 0,
                "link_target": "missing.py",
            }
        )
        broken_entries.sort(key=lambda entry: str(entry["path"]))
        broken_archive = self._archive(
            [
                {"path": "app/comfyui", "type": "directory", "mode": 0o755},
                {"path": "app/comfyui/main.py", "type": "file", "mode": 0o755, "payload": payload},
                {
                    "path": "app/comfyui/broken",
                    "type": "symlink",
                    "mode": 0o777,
                    "link_target": "missing.py",
                },
            ],
            name_prefix="broken-symlink",
        )
        broken_manifest = self._manifest(broken_entries, broken_archive, runtime_version="broken-symlink")
        with self.assertRaisesRegex(materializer.RuntimeMaterializerError, "verification_symlink"):
            materializer.materialize_runtime(broken_archive, broken_manifest, self.volume)

    def test_traversal_hardlink_and_special_members_are_rejected(self) -> None:
        cases = [
            (
                "traversal",
                [{"path": "../outside", "type": "file", "mode": 0o644, "payload": b"x"}],
                "unsafe_member",
            ),
            (
                "hardlink",
                [
                    {"path": "app/comfyui", "type": "directory", "mode": 0o755},
                    {"path": "app/comfyui/main.py", "type": "hardlink", "mode": 0o755},
                ],
                "hardlink_rejected",
            ),
            (
                "special",
                [
                    {"path": "app/comfyui", "type": "directory", "mode": 0o755},
                    {"path": "app/comfyui/main.py", "type": "fifo", "mode": 0o755},
                ],
                "special_member",
            ),
        ]
        for name, members, expected_error in cases:
            with self.subTest(name=name):
                entries = self._entries()
                archive = self._archive(members, name_prefix=name)
                manifest = self._manifest(entries, archive, runtime_version=name)
                with self.assertRaisesRegex(materializer.RuntimeMaterializerError, expected_error):
                    materializer.materialize_runtime(archive, manifest, self.volume)
        self.assertFalse((self.volume / "runtimes" / "current").exists())

    def test_mode_size_and_digest_mismatches_are_rejected(self) -> None:
        entries = self._entries()
        cases = [
            ("mode", {"mode": 0o644, "payload": b"runtime"}, "member_mode_mismatch"),
            ("size", {"mode": 0o755, "payload": b"different"}, "member_size_mismatch"),
            ("digest", {"mode": 0o755, "payload": b"different"}, "member_size_mismatch"),
        ]
        for name, altered, expected_error in cases:
            with self.subTest(name=name):
                archive = self._archive(
                    [
                        {"path": "app/comfyui", "type": "directory", "mode": 0o755},
                        {"path": "app/comfyui/main.py", "type": "file", **altered},
                    ],
                    name_prefix=f"mismatch-{name}",
                )
                manifest = self._manifest(entries, archive, runtime_version=f"mismatch-{name}")
                with self.assertRaisesRegex(materializer.RuntimeMaterializerError, expected_error):
                    materializer.materialize_runtime(archive, manifest, self.volume)

    def test_failed_current_update_keeps_old_current_and_new_generation_immutable(self) -> None:
        old_archive, old_manifest, _ = self._valid_inputs()
        old_result = materializer.materialize_runtime(old_archive, old_manifest, self.volume)
        current = self.volume / "runtimes" / "current"
        old_target = current.readlink()

        entries = self._entries(b"new")
        new_archive = self._archive(
            [
                {"path": "app/comfyui", "type": "directory", "mode": 0o755},
                {"path": "app/comfyui/main.py", "type": "file", "mode": 0o755, "payload": b"new"},
            ],
            name_prefix="new",
        )
        new_manifest = self._manifest(entries, new_archive, runtime_version="new")
        current.unlink()
        current.mkdir()
        with self.assertRaisesRegex(materializer.RuntimeMaterializerError, "current_not_symlink"):
            materializer.materialize_runtime(new_archive, new_manifest, self.volume)
        self.assertTrue(current.is_dir())
        new_runtime_digest = materializer._read_manifest(new_manifest)[1]["runtime_digest"]
        self.assertNotEqual(old_result["runtime_digest"], new_runtime_digest)
        runtime_hex = new_runtime_digest[len("sha256:") :]
        self.assertTrue((self.volume / "runtimes" / runtime_hex / "READY.json").is_file())
        current.rmdir()
        current.symlink_to(old_target)
        self.assertEqual(current.readlink(), old_target)

    def test_cli_errors_are_bounded_json_without_input_paths(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = materializer.main(
                [
                    "--archive",
                    str(self.root / "missing.tar.zst"),
                    "--manifest",
                    str(self.root / "missing.json"),
                    "--volume-root",
                    str(self.volume),
                ]
            )
        self.assertEqual(result, 2)
        output = json.loads(stdout.getvalue())
        self.assertEqual(output["status"], "error")
        self.assertNotIn(str(self.root), stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
