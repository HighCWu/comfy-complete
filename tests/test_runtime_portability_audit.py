"""Focused tests for the read-only runtime portability audit."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "runtime_portability_audit", ROOT / "scripts" / "runtime_portability_audit.py"
)
assert SPEC and SPEC.loader
audit = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = audit
SPEC.loader.exec_module(audit)


class RuntimePortabilityAuditTests(unittest.TestCase):
    def rootfs(self) -> Path:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        (root / "opt/conda/bin").mkdir(parents=True)
        (root / "opt/conda/lib").mkdir(parents=True)
        (root / "app/comfyui/bin").mkdir(parents=True)
        return root

    def audit_root(self, root: Path, *, inventory: audit.LauncherInventory | None = None):
        return audit.audit_runtime(
            source_root=root,
            targets=["/opt/conda", "/app/comfyui"],
            app_files=[],
            exclusions=[],
            launcher_inventory=inventory,
        )

    def copy_true(self, root: Path, name: str = "true") -> Path:
        destination = root / "app/comfyui/bin" / name
        destination.write_bytes(Path("/bin/true").read_bytes())
        destination.chmod(0o755)
        return destination

    def compile_rpath_binary(self, root: Path, *, name: str, rpath: str, old_dtags: bool = False) -> Path:
        compiler = shutil.which("cc") or shutil.which("gcc")
        if compiler is None:
            self.skipTest("C compiler is unavailable")
        source = root / f"{name}.c"
        source.write_text("int main(void) { return 0; }\n", encoding="utf-8")
        destination = root / "app/comfyui/bin" / name
        command = [compiler, str(source), f"-Wl,-rpath,{rpath}"]
        if old_dtags:
            command.append("-Wl,--disable-new-dtags")
        command.extend(["-o", str(destination)])
        subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        destination.chmod(0o755)
        return destination

    def test_absolute_shebang_missing_is_blocker(self) -> None:
        with tempfile.TemporaryDirectory():
            root = self.rootfs()
            script = root / "app/comfyui/bin/script"
            script.write_text("#!/opt/conda/bin/missing-python\n", encoding="utf-8")
            script.chmod(0o755)
            report = self.audit_root(root)
            self.assertEqual(report["status"], "blocker")
            self.assertIn("/opt/conda/bin/missing-python", report["launcher_image_requirements"]["system_paths"])
            self.assertTrue(any(item["code"] == "missing_absolute_shebang" for item in report["findings"]))

    def test_absolute_shebang_launcher_symlink_is_provided(self) -> None:
        with tempfile.TemporaryDirectory():
            root = self.rootfs()
            script = root / "app/comfyui/bin/script"
            script.write_text("#!/opt/conda/bin/python\n", encoding="utf-8")
            script.chmod(0o755)
            inventory = audit.LauncherInventory(
                system_paths=("/usr/bin/python3",),
                symlinks=(("/opt/conda/bin/python", "/usr/bin/python3"),),
                executable_paths=("/usr/bin/python3",),
            )
            report = self.audit_root(root, inventory=inventory)
            self.assertFalse(any(item["code"] == "missing_absolute_shebang" for item in report["findings"]))
            self.assertTrue(any(item["code"] == "absolute_shebang_resolved" for item in report["findings"]))

    def test_absolute_shebang_to_other_path_does_not_use_unrelated_inventory(self) -> None:
        with tempfile.TemporaryDirectory():
            root = self.rootfs()
            script = root / "app/comfyui/bin/script"
            script.write_text("#!/opt/conda/bin/python\n", encoding="utf-8")
            script.chmod(0o755)
            inventory = audit.LauncherInventory(system_paths=("/usr/bin/python3",))
            report = self.audit_root(root, inventory=inventory)
            self.assertEqual(report["status"], "blocker")
            self.assertTrue(any(item["code"] == "missing_absolute_shebang" for item in report["findings"]))

    def test_shell_and_python_script_fixtures_are_scanned(self) -> None:
        with tempfile.TemporaryDirectory():
            root = self.rootfs()
            for name in ("check.sh", "check.py"):
                script = root / "app/comfyui/bin" / name
                script.write_text("#!/opt/conda/bin/missing-python\n", encoding="utf-8")
                script.chmod(0o755)
            report = self.audit_root(root)
            missing = [item for item in report["findings"] if item["code"] == "missing_absolute_shebang"]
            self.assertEqual(len(missing), 2)

    def test_real_elf_reports_interpreter_and_needed(self) -> None:
        with tempfile.TemporaryDirectory():
            root = self.rootfs()
            self.copy_true(root)
            inventory = audit.LauncherInventory(
                system_paths=("/lib64/ld-linux-x86-64.so.2",),
                libraries=("libc.so.6",),
                executable_paths=("/lib64/ld-linux-x86-64.so.2",),
            )
            report = self.audit_root(root, inventory=inventory)
            elf = report["elf"]["files"][0]
            self.assertEqual(elf["interpreter"], "/lib64/ld-linux-x86-64.so.2")
            self.assertIn("libc.so.6", elf["needed"])
            self.assertEqual(report["summary"]["elf_files"], 1)

    def test_missing_elf_interpreter_and_library_are_blockers(self) -> None:
        with tempfile.TemporaryDirectory():
            root = self.rootfs()
            self.copy_true(root)
            report = self.audit_root(root)
            codes = {item["code"] for item in report["findings"]}
            self.assertIn("missing_elf_interpreter", codes)
            self.assertIn("missing_elf_library", codes)

    def test_env_shebang_requires_executable_env_then_warns_on_path_dependency(self) -> None:
        with tempfile.TemporaryDirectory():
            root = self.rootfs()
            script = root / "app/comfyui/bin/env-script"
            script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            script.chmod(0o755)
            missing = self.audit_root(root)
            self.assertTrue(any(item["code"] == "missing_env_interpreter" for item in missing["findings"]))
            provided = self.audit_root(
                root,
                inventory=audit.LauncherInventory(
                    system_paths=("/usr/bin/env",),
                    executable_paths=("/usr/bin/env",),
                ),
            )
            self.assertFalse(any(item["code"] == "missing_env_interpreter" for item in provided["findings"]))
            self.assertTrue(any(item["code"] == "path_dependent_shebang" for item in provided["findings"]))

    def test_runpath_is_reported_and_missing_directory_is_required(self) -> None:
        with tempfile.TemporaryDirectory():
            root = self.rootfs()
            self.compile_rpath_binary(root, name="runpath", rpath="/launcher/missing")
            report = self.audit_root(root)
            elf = next(item for item in report["elf"]["files"] if item["path"].endswith("/runpath"))
            self.assertEqual(elf["runpath"], ["/launcher/missing"])
            self.assertNotIn("/launcher/missing", report["launcher_image_requirements"]["library_search_paths"])
            self.assertTrue(any(item["code"] == "missing_elf_search_path" and item["severity"] == "warning" for item in report["findings"]))

    def test_origin_relative_runpath_is_expanded_to_selected_directory(self) -> None:
        with tempfile.TemporaryDirectory():
            root = self.rootfs()
            self.compile_rpath_binary(root, name="origin-runpath", rpath="$ORIGIN/../lib")
            (root / "app/comfyui/lib").mkdir()
            report = self.audit_root(root)
            elf = next(item for item in report["elf"]["files"] if item["path"].endswith("/origin-runpath"))
            self.assertEqual(elf["runpath"], ["$ORIGIN/../lib"])
            self.assertFalse(any(item["code"] == "unresolved_elf_search_path" and item["path"].endswith("/origin-runpath") for item in report["findings"]))
            self.assertTrue(any(item["code"] == "elf_search_path_resolved" and item["path"].endswith("/origin-runpath") for item in report["findings"]))

    def test_rpath_is_reported(self) -> None:
        with tempfile.TemporaryDirectory():
            root = self.rootfs()
            self.compile_rpath_binary(root, name="rpath", rpath="/launcher/legacy", old_dtags=True)
            report = self.audit_root(root)
            elf = next(item for item in report["elf"]["files"] if item["path"].endswith("/rpath"))
            self.assertEqual(elf["rpath"], ["/launcher/legacy"])

    def test_same_named_library_in_unsearched_selected_directory_is_runtime_unresolved(self) -> None:
        with tempfile.TemporaryDirectory():
            root = self.rootfs()
            self.copy_true(root)
            (root / "opt/conda/lib/libc.so.6").write_bytes(b"not the dependency")
            report = self.audit_root(
                root,
                inventory=audit.LauncherInventory(
                    system_paths=("/lib64/ld-linux-x86-64.so.2",),
                    executable_paths=("/lib64/ld-linux-x86-64.so.2",),
                ),
            )
            self.assertTrue(any(item["code"] == "runtime_library_present_unresolved" and item["evidence"]["needed"] == "libc.so.6" for item in report["findings"]))
            self.assertNotIn("libc.so.6", report["launcher_image_requirements"]["libraries"])

    def test_missing_library_is_launcher_requirement_when_runtime_has_no_candidate(self) -> None:
        with tempfile.TemporaryDirectory():
            root = self.rootfs()
            self.copy_true(root)
            report = self.audit_root(root)
            self.assertTrue(any(item["code"] == "missing_elf_library" and item["evidence"]["needed"] == "libc.so.6" for item in report["findings"]))
            self.assertIn("libc.so.6", report["launcher_image_requirements"]["libraries"])

    def test_selection_policy_excludes_mutable_tree_and_counts_symlink(self) -> None:
        with tempfile.TemporaryDirectory():
            root = self.rootfs()
            self.copy_true(root)
            (root / "app/comfyui/output/ignored").parent.mkdir(parents=True)
            (root / "app/comfyui/output/ignored").write_bytes(b"ignored")
            os.symlink("true", root / "app/comfyui/bin/true-link")
            report = audit.audit_runtime(
                source_root=root,
                targets=["/opt/conda", "/app/comfyui"],
                exclusions=["/app/comfyui/output"],
            )
            self.assertEqual(report["symlinks"]["total"], 1)
            self.assertNotIn("app/comfyui/output/ignored", {item["path"] for item in report["elf"]["files"]})

    def test_report_is_deterministic_and_rootfs_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory():
            root = self.rootfs()
            self.copy_true(root)
            before = sorted((path.relative_to(root).as_posix(), path.stat().st_mtime_ns) for path in root.rglob("*") if not path.is_symlink())
            first = audit.render_report(self.audit_root(root))
            second = audit.render_report(self.audit_root(root))
            after = sorted((path.relative_to(root).as_posix(), path.stat().st_mtime_ns) for path in root.rglob("*") if not path.is_symlink())
            self.assertEqual(first, second)
            self.assertEqual(before, after)
            self.assertEqual(json.loads(first)["schema_version"], 1)

    def test_readelf_missing_is_explicitly_limited(self) -> None:
        with tempfile.TemporaryDirectory():
            root = self.rootfs()
            self.copy_true(root)
            with patch.object(audit.shutil, "which", return_value=None):
                report = self.audit_root(root)
            self.assertEqual(report["status"], "limited")
            self.assertEqual(report["tooling"]["elf_analysis"], "limited")
            self.assertTrue(any(item["code"] == "readelf_unavailable_or_failed" for item in report["findings"]))

    def test_readelf_missing_cli_exit_is_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self.rootfs()
            self.copy_true(root)
            output = Path(directory) / "report.json"
            with patch.object(audit.shutil, "which", return_value=None):
                exit_code = audit.main(["--source-root", str(root), "--output", str(output)])
            self.assertNotEqual(exit_code, 0)

    def test_empty_rpath_and_runpath_are_not_unresolved_paths(self) -> None:
        metadata = audit._elf_metadata(
            """
            Requesting program interpreter: /lib64/ld-linux-x86-64.so.2]
            0x000000000000000f (RPATH)              Library rpath: []
            0x000000000000001d (RUNPATH)            Library runpath: []
            """
        )
        self.assertEqual(metadata["rpath"], [])
        self.assertEqual(metadata["runpath"], [])

    def test_empty_search_path_component_is_blocker_not_launcher_requirement(self) -> None:
        with tempfile.TemporaryDirectory():
            root = self.rootfs()
            self.copy_true(root)
            output = """
                Requesting program interpreter: /lib64/ld-linux-x86-64.so.2]
                0x000000000000000f (RPATH)              Library rpath: [/launcher/legacy:]
                0x0000000000000001 (NEEDED)             Shared library: [libc.so.6]
            """
            with patch.object(audit, "_readelf", return_value=(output, True)):
                report = self.audit_root(root)
            self.assertTrue(any(item["code"] == "implicit_cwd_elf_search_path" for item in report["findings"]))
            self.assertNotIn("", report["launcher_image_requirements"]["library_search_paths"])
            self.assertNotIn("/launcher/legacy", report["launcher_image_requirements"]["library_search_paths"])

    def test_empty_interpreter_and_needed_are_rejected(self) -> None:
        with self.assertRaisesRegex(audit.RuntimeAuditError, "unsafe interpreter"):
            audit._elf_metadata("Requesting program interpreter: ]")
        with self.assertRaisesRegex(audit.RuntimeAuditError, "unsafe library"):
            audit._elf_metadata("Shared library: []")

    def test_invalid_readelf_metadata_is_reported_without_discarding_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self.rootfs()
            self.copy_true(root)
            output = Path(directory) / "report.json"
            with patch.object(audit, "_elf_metadata", side_effect=audit.RuntimeAuditError("unsafe fixture")):
                exit_code = audit.main(["--source-root", str(root), "--output", str(output)])
            self.assertNotEqual(exit_code, 0)
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "limited")
            self.assertTrue(any(item["code"] == "readelf_metadata_invalid" for item in report["findings"]))

if __name__ == "__main__":
    unittest.main()
