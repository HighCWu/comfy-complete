"""Focused tests for the read-only runtime portability audit."""

from __future__ import annotations

import importlib.util
import hashlib
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

PROBE_SPEC = importlib.util.spec_from_file_location(
    "runtime_critical_probe", ROOT / "scripts" / "runtime_critical_probe.py"
)
assert PROBE_SPEC and PROBE_SPEC.loader
probe = importlib.util.module_from_spec(PROBE_SPEC)
sys.modules[PROBE_SPEC.name] = probe
PROBE_SPEC.loader.exec_module(probe)


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
            unresolved = next(item for item in report["findings"] if item["code"] == "runtime_library_present_unresolved")
            self.assertEqual(unresolved["severity"], "warning")

    def test_portability_gate_is_deny_by_default_and_caps_allowances(self) -> None:
        findings = [
            {
                "code": "missing_elf_library",
                "severity": "blocker",
                "path": "app/comfyui/optional/tool",
                "detail": "optional",
            },
            {
                "code": "missing_elf_library",
                "severity": "blocker",
                "path": "app/comfyui/optional/new-tool",
                "detail": "new",
            },
            {
                "code": "missing_elf_library",
                "severity": "blocker",
                "path": "opt/conda/bin/python",
                "detail": "core",
            },
        ]
        reviewed = audit.portability_finding_fingerprint(findings[0])
        policy = audit.validate_portability_gate_policy(
            {
                "schema_version": audit.PORTABILITY_GATE_POLICY_VERSION,
                "audit_version": audit.AUDIT_VERSION,
                "critical_profile": "base",
                "profile": "fixture",
                "default_action": "block",
                "allowances": [
                    {
                        "id": "optional-fixture",
                        "finding_fingerprints": [reviewed],
                        "max_matches": 1,
                        "reason": "fixture optional dependency",
                    }
                ],
            }
        )
        gate = audit.evaluate_portability_gate(findings, policy)
        self.assertEqual(gate["status"], "blocker")
        self.assertEqual(gate["raw_blocker_count"], 3)
        self.assertEqual(gate["allowed_blocker_count"], 1)
        self.assertEqual(gate["unapproved_blocker_count"], 2)
        self.assertEqual(
            {item["gate_reason"] for item in gate["unapproved_findings"]},
            {"no_allowance_match"},
        )

    def test_portability_gate_policy_is_bound_to_scanner_and_critical_profile(self) -> None:
        base = {
            "schema_version": audit.PORTABILITY_GATE_POLICY_VERSION,
            "audit_version": audit.AUDIT_VERSION,
            "critical_profile": "base",
            "profile": "fixture",
            "default_action": "block",
            "allowances": [],
        }
        audit.validate_portability_gate_policy(base)
        with self.assertRaisesRegex(audit.RuntimeAuditError, "audit_version"):
            audit.validate_portability_gate_policy({**base, "audit_version": "older-scanner"})
        with self.assertRaisesRegex(audit.RuntimeAuditError, "critical_profile"):
            audit.validate_portability_gate_policy({**base, "critical_profile": "other"})

    def test_portability_gate_matches_the_complete_finding_identity(self) -> None:
        approved = [
            {"code": "missing_absolute_shebang", "severity": "blocker", "path": "app/x/.git/hooks/pre-commit", "evidence": {"interpreter": "/usr/bin/perl"}},
            {"code": "missing_absolute_shebang", "severity": "blocker", "path": "app/x/cgi.py", "evidence": {"interpreter": "/usr/local/bin/python"}},
        ]
        policy = audit.validate_portability_gate_policy(
            {
                "schema_version": audit.PORTABILITY_GATE_POLICY_VERSION,
                "audit_version": audit.AUDIT_VERSION,
                "critical_profile": "base",
                "profile": "fixture",
                "default_action": "block",
                "allowances": [
                    {
                        "id": "hook",
                        "finding_fingerprints": [audit.portability_finding_fingerprint(approved[0])],
                        "max_matches": 1,
                        "reason": "fixture metadata",
                    },
                    {
                        "id": "cgi",
                        "finding_fingerprints": [audit.portability_finding_fingerprint(approved[1])],
                        "max_matches": 1,
                        "reason": "fixture dead module header",
                    },
                ],
            }
        )
        findings = [*approved,
            {"code": "missing_absolute_shebang", "severity": "blocker", "path": "app/x/cgi.py", "evidence": {"interpreter": "/usr/bin/python"}},
        ]
        gate = audit.evaluate_portability_gate(findings, policy)
        self.assertEqual(gate["status"], "blocker")
        self.assertEqual(gate["allowed_blocker_count"], 2)
        self.assertEqual(gate["unapproved_blocker_count"], 1)

    def test_portability_gate_rejects_order_dependent_overlapping_allowances(self) -> None:
        finding = {
            "code": "missing_elf_library",
            "severity": "blocker",
            "path": "app/comfyui/optional/tool/plugin.so",
            "detail": "overlap",
        }
        fingerprint = audit.portability_finding_fingerprint(finding)
        policy = audit.validate_portability_gate_policy(
            {
                "schema_version": audit.PORTABILITY_GATE_POLICY_VERSION,
                "audit_version": audit.AUDIT_VERSION,
                "critical_profile": "base",
                "profile": "fixture",
                "default_action": "block",
                "allowances": [
                    {
                        "id": "broad",
                        "finding_fingerprints": [fingerprint],
                        "max_matches": 1,
                        "reason": "broad fixture",
                    },
                    {
                        "id": "narrow",
                        "finding_fingerprints": [fingerprint],
                        "max_matches": 1,
                        "reason": "narrow fixture",
                    },
                ],
            }
        )
        gate = audit.evaluate_portability_gate([finding], policy)
        self.assertEqual(gate["status"], "blocker")
        self.assertEqual(gate["allowed_blocker_count"], 0)
        self.assertEqual(gate["unapproved_blocker_count"], 1)
        self.assertEqual(
            gate["unapproved_findings"][0]["gate_reason"],
            "ambiguous_allowance_match",
        )
        self.assertEqual(gate["unapproved_findings"][0]["allowance_ids"], ["broad", "narrow"])

    def test_truncated_static_gate_has_one_consistent_incomplete_finding(self) -> None:
        with tempfile.TemporaryDirectory():
            root = self.rootfs()
            for index in range(3):
                script = root / f"app/comfyui/tool-{index}"
                script.write_text("#!/missing/interpreter\n", encoding="utf-8")
                script.chmod(0o755)
            policy = audit.validate_portability_gate_policy(
                {
                    "schema_version": audit.PORTABILITY_GATE_POLICY_VERSION,
                    "audit_version": audit.AUDIT_VERSION,
                    "critical_profile": "base",
                    "profile": "deny-all",
                    "default_action": "block",
                    "allowances": [],
                }
            )
            report = audit.audit_runtime(
                source_root=root,
                limits=audit.Limits(max_findings=1),
                portability_gate_policy=policy,
            )
            gate = report["gate"]["static_policy"]
            incomplete = [
                item
                for item in gate["unapproved_findings"]
                if item["code"] == "finding_limit_exceeded"
            ]
            self.assertEqual(len(incomplete), 1)
            self.assertEqual(gate["raw_blocker_count"], gate["unapproved_blocker_count"])
            self.assertEqual(gate["allowed_blocker_count"], 0)

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
            parsed = json.loads(first)
            self.assertEqual(parsed["schema_version"], 2)
            self.assertEqual(parsed["gate"]["source"], "whole_rootfs")
            self.assertEqual(parsed["gate"]["status"], parsed["status"])

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

    def test_critical_gate_can_pass_while_whole_rootfs_inventory_stays_blocked(self) -> None:
        root = self.rootfs()
        interpreter = root / "opt/conda/bin/python"
        interpreter.write_bytes(Path("/bin/true").read_bytes())
        interpreter.chmod(0o755)
        (root / "app/comfyui/main.py").write_text("value = 1\n", encoding="utf-8")
        optional = root / "app/comfyui/bin/optional-tool"
        optional.write_text("#!/missing/optional-interpreter\n", encoding="utf-8")
        optional.chmod(0o755)
        config = audit.load_critical_config(ROOT / "ci/runtime-critical-entrypoints.json")
        with patch.object(probe.os, "chdir"), patch.object(
            probe,
            "_compile_main_script",
            return_value={"path": "/app/comfyui/main.py", "status": "pass", "source_bytes": 10},
        ), patch.object(probe, "_import_one", side_effect=lambda module, required, profile: {
            "module": module,
            "required": required,
            "profile": profile,
            "status": "pass",
            "duration_ms": 1,
            "before_shared_objects": [],
            "before_shared_object_classification": {"runtime": [], "launcher_or_system": [], "other": []},
            "before_mapped_files": [],
            "cumulative_shared_objects": [],
            "cumulative_shared_object_classification": {"runtime": [], "launcher_or_system": [], "other": []},
            "cumulative_mapped_files": [],
            "new_shared_objects": [],
            "new_shared_object_classification": {"runtime": [], "launcher_or_system": [], "other": []},
            "new_mapped_files": [],
            "stderr": "",
            "stdout": "",
        }):
            critical_probe = probe.run_probe(config, "cpu")
        metadata = audit._elf_metadata(
            subprocess.run(
                ["readelf", "-lW", "-dW", str(interpreter)],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            ).stdout
        )
        inventory = audit.LauncherInventory(
            system_paths=(metadata["interpreter"],),
            executable_paths=(metadata["interpreter"],),
            libraries=tuple(metadata["needed"]),
        )
        report = audit.audit_runtime(
            source_root=root,
            targets=["/opt/conda", "/app/comfyui"],
            exclusions=[],
            launcher_inventory=inventory,
            critical_config=config,
            critical_probe=critical_probe,
            critical_profile="cpu",
        )
        self.assertEqual(report["status"], "blocker")
        self.assertEqual(report["critical"]["status"], "partial")
        self.assertEqual(report["gate"], {
            "status": "pass",
            "source": "critical",
            "profile": "base",
            "probe_profile": "cpu",
            "evidence_status": "partial",
        })

    def test_critical_blocker_controls_gate_even_when_whole_rootfs_passes(self) -> None:
        root = self.rootfs()
        config = audit.load_critical_config(ROOT / "ci/runtime-critical-entrypoints.json")
        report = audit.audit_runtime(
            source_root=root,
            targets=["/opt/conda", "/app/comfyui"],
            exclusions=[],
            critical_config=config,
            critical_probe=None,
            critical_profile="cpu",
        )
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["critical"]["status"], "blocker")
        self.assertEqual(report["gate"]["status"], "blocker")
        self.assertEqual(report["gate"]["source"], "critical")

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

    def test_critical_config_is_strict_and_allowlists_imports(self) -> None:
        config = json.loads((ROOT / "ci/runtime-critical-entrypoints.json").read_text(encoding="utf-8"))
        validated = audit.validate_critical_config(config)
        self.assertEqual(validated["interpreter"], "/opt/conda/bin/python")
        bad = json.loads(json.dumps(config))
        bad["imports"][0]["module"] = "os.system"
        with self.assertRaises(audit.RuntimeAuditError):
            audit.validate_critical_config(bad)
        bad = json.loads(json.dumps(config))
        bad["imports"].append({"module": "torch", "required": True, "profile": "cpu"})
        with self.assertRaises(audit.RuntimeAuditError):
            audit.validate_critical_config(bad)
        bad = json.loads(json.dumps(config))
        bad["schema_version"] = 1
        with self.assertRaises(audit.RuntimeAuditError):
            audit.validate_critical_config(bad)

        self.assertEqual(config["default_probe_profile"], "cpu")
        profiles = {item["module"]: item["profile"] for item in validated["imports"]}
        self.assertEqual(profiles["execution"], "gpu_required")
        self.assertEqual(profiles["server"], "gpu_required")
        self.assertEqual(
            {item["module"] for item in validated["imports"] if item["profile"] == "cpu"},
            {"PIL", "aiohttp", "comfy", "folder_paths", "numpy", "torch"},
        )

    def test_critical_entrypoint_finds_real_root_files_outside_selection(self) -> None:
        with tempfile.TemporaryDirectory():
            root = self.rootfs()
            (root / "app/entrypoint.sh").write_text("#!/bin/bash\n", encoding="utf-8")
            (root / "app/entrypoint.sh").chmod(0o755)
            config = json.loads((ROOT / "ci/runtime-critical-entrypoints.json").read_text(encoding="utf-8"))
            inventory = audit.LauncherInventory(
                system_paths=("/bin/bash", "/usr/bin/dumb-init"),
                executable_paths=("/bin/bash", "/usr/bin/dumb-init"),
            )
            report = self.audit_root(root, inventory=inventory)
            report["critical"] = audit._critical_report(config, root, {"/" + item.path: item for item in audit.collect_records(root, report["selection_policy"], audit.Limits())[0]}, inventory, audit.Limits(), None)
            self.assertFalse(any(item["code"] == "critical_entrypoint_shebang_mismatch" and item["path"] == "/app/entrypoint.sh" for item in report["critical"]["findings"]))

    def test_critical_probe_schema_rejects_control_and_unapproved_imports(self) -> None:
        probe = {
            "schema_version": 3,
            "profile": "base",
            "probe_profile": "cpu",
            "config_sha256": "0" * 64,
            "status": "blocker",
            "coverage": "incomplete",
            "policy": dict(audit.CRITICAL_POLICY),
            "main_script_compile": {"path": "/app/comfyui/main.py", "status": "failed", "source_bytes": 0, "error": "fixture"},
            "import_review": [],
            "imports": [],
        }
        with tempfile.NamedTemporaryFile("w", suffix=".json") as handle:
            json.dump(probe, handle)
            handle.flush()
            probe["policy"]["writable_paths"] = ["/tmp", "../audit"]
            handle.seek(0)
            handle.truncate()
            json.dump(probe, handle)
            handle.flush()
            with self.assertRaises(audit.RuntimeAuditError):
                audit.load_critical_probe(Path(handle.name))

    def test_critical_probe_records_mapped_shared_objects_and_failures(self) -> None:
        with patch.object(probe.importlib, "import_module", side_effect=RuntimeError("fixture import failure")):
            with patch.object(probe, "_mapped_files", side_effect=[["/lib64/ld-before.so"], ["/opt/conda/lib/libfixture.so", "/lib64/ld-before.so", "/lib64/ld-fixture.so"]]):
                result = probe._import_one("torch", True)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["before_shared_objects"], ["/lib64/ld-before.so"])
        self.assertEqual(result["cumulative_shared_objects"], ["/lib64/ld-before.so", "/lib64/ld-fixture.so", "/opt/conda/lib/libfixture.so"])
        self.assertEqual(result["new_shared_objects"], ["/lib64/ld-fixture.so", "/opt/conda/lib/libfixture.so"])
        self.assertEqual(result["cumulative_shared_object_classification"]["runtime"], ["/opt/conda/lib/libfixture.so"])
        self.assertEqual(result["cumulative_shared_object_classification"]["launcher_or_system"], ["/lib64/ld-before.so", "/lib64/ld-fixture.so"])
        self.assertEqual(result["new_shared_object_classification"]["launcher_or_system"], ["/lib64/ld-fixture.so"])
        self.assertIn("fixture import failure", result["stderr"])

    def test_critical_probe_runs_only_configured_allowlisted_imports(self) -> None:
        config = json.loads((ROOT / "ci/runtime-critical-entrypoints.json").read_text(encoding="utf-8"))
        imported: list[tuple[str, bool]] = []

        def fake_import(module: str, required: bool, profile: str) -> dict[str, object]:
            imported.append((module, required))
            return {
                "module": module,
                "required": required,
                "profile": profile,
                "status": "pass",
                "duration_ms": 1,
                "before_shared_objects": [],
                "before_shared_object_classification": {"runtime": [], "launcher_or_system": [], "other": []},
                "before_mapped_files": [],
                "cumulative_shared_objects": [],
                "cumulative_shared_object_classification": {"runtime": [], "launcher_or_system": [], "other": []},
                "cumulative_mapped_files": [],
                "new_shared_objects": [],
                "new_shared_object_classification": {"runtime": [], "launcher_or_system": [], "other": []},
                "new_mapped_files": [],
                "stderr": "",
                "stdout": "",
            }

        with patch.object(probe.os, "chdir"), patch.object(probe, "_compile_main_script", return_value={"path": "/app/comfyui/main.py", "status": "pass", "source_bytes": 1}), patch.object(probe, "_import_one", side_effect=fake_import), patch.dict(
            probe.os.environ, {}, clear=False
        ):
            report = probe.run_probe(config)
        self.assertEqual(report["status"], "partial")
        self.assertEqual(
            imported,
            [(item["module"], item["required"]) for item in config["imports"] if item["profile"] == "cpu"],
        )
        self.assertEqual(
            {item["module"] for item in report["imports"] if item["status"] == "not_executed"},
            {"execution", "server"},
        )
        self.assertTrue(all(item["reason_code"] == "environment_unavailable" for item in report["imports"] if item["status"] == "not_executed"))
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["coverage"], "partial")
        self.assertEqual(report["main_script_compile"]["status"], "pass")
        self.assertEqual(report["config_sha256"], hashlib.sha256(json.dumps(config, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest())

    def test_gpu_probe_preserves_provider_device_visibility(self) -> None:
        config = json.loads((ROOT / "ci/runtime-critical-entrypoints.json").read_text(encoding="utf-8"))
        with patch.object(probe.os, "chdir"), patch.object(
            probe,
            "_compile_main_script",
            return_value={"path": "/app/comfyui/main.py", "status": "pass", "source_bytes": 1},
        ), patch.object(probe, "_import_one", side_effect=lambda module, required, profile: {
            "module": module,
            "required": required,
            "profile": profile,
            "status": "pass",
            "duration_ms": 1,
            "before_shared_objects": [],
            "before_shared_object_classification": {"runtime": [], "launcher_or_system": [], "other": []},
            "before_mapped_files": [],
            "cumulative_shared_objects": [],
            "cumulative_shared_object_classification": {"runtime": [], "launcher_or_system": [], "other": []},
            "cumulative_mapped_files": [],
            "new_shared_objects": [],
            "new_shared_object_classification": {"runtime": [], "launcher_or_system": [], "other": []},
            "new_mapped_files": [],
            "stderr": "",
            "stdout": "",
        }), patch.dict(probe.os.environ, {}, clear=True):
            report = probe.run_probe(config, "gpu_required")
            self.assertNotIn("CUDA_VISIBLE_DEVICES", probe.os.environ)
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["coverage"], "complete")

    def test_critical_probe_blocks_uninventoried_external_shared_objects(self) -> None:
        root = self.rootfs()
        config = audit.load_critical_config(ROOT / "ci/runtime-critical-entrypoints.json")
        probe_report = {
            "schema_version": 3,
            "profile": "base",
            "probe_profile": "cpu",
            "config_sha256": hashlib.sha256(json.dumps(config, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest(),
            "status": "partial",
            "coverage": "partial",
            "policy": dict(audit.CRITICAL_POLICY),
            "main_script_compile": {"path": "/app/comfyui/main.py", "status": "pass", "source_bytes": 1},
            "import_review": config["import_review"],
            "imports": [],
        }
        for item in config["imports"]:
            result = probe._not_executed(item["module"], item["required"], item["profile"])
            if item["profile"] == "cpu":
                result.update({
                    "status": "pass",
                    "reason_code": None,
                    "cumulative_shared_objects": ["/usr/local/cuda/lib/libfixture.so"],
                    "cumulative_shared_object_classification": {
                        "runtime": [],
                        "launcher_or_system": [],
                        "other": ["/usr/local/cuda/lib/libfixture.so"],
                    },
                })
                result.pop("reason_code")
            probe_report["imports"].append(result)
        report = audit._critical_report(config, root, {}, audit.LauncherInventory(), audit.Limits(), probe_report, "cpu")
        findings = [item for item in report["findings"] if item["code"] == "critical_probe_unprovided_shared_object"]
        self.assertEqual({item["path"] for item in findings}, {"PIL", "aiohttp", "comfy", "folder_paths", "numpy", "torch"})
        self.assertTrue(all(item["evidence"]["mapped_path"] == "/usr/local/cuda/lib/libfixture.so" for item in findings))

    def test_critical_probe_schema_rejects_duplicate_imports_and_noncanonical_paths(self) -> None:
        config = json.loads((ROOT / "ci/runtime-critical-entrypoints.json").read_text(encoding="utf-8"))

        with patch.object(probe.os, "chdir"), patch.object(
            probe, "_compile_main_script", return_value={"path": "/app/comfyui/main.py", "status": "pass", "source_bytes": 1}
        ), patch.object(
            probe, "_import_one", return_value={
                "module": "PIL",
                "required": True,
                "profile": "cpu",
                "status": "pass",
                "duration_ms": 1,
                "before_shared_objects": [],
                "before_shared_object_classification": {"runtime": [], "launcher_or_system": [], "other": []},
                "before_mapped_files": [],
                "cumulative_shared_objects": [],
                "cumulative_shared_object_classification": {"runtime": [], "launcher_or_system": [], "other": []},
                "cumulative_mapped_files": [],
                "new_shared_objects": [],
                "new_shared_object_classification": {"runtime": [], "launcher_or_system": [], "other": []},
                "new_mapped_files": [],
                "stderr": "",
                "stdout": "",
            }
        ):
            report = probe.run_probe({**config, "imports": [{"module": "PIL", "required": True, "profile": "cpu"}], "import_review": [config["import_review"][0]]})
        with tempfile.NamedTemporaryFile("w", suffix=".json") as handle:
            json.dump(report, handle)
            handle.flush()
            audit.load_critical_probe(Path(handle.name))
            report["imports"].append(dict(report["imports"][0]))
            handle.seek(0)
            handle.truncate()
            json.dump(report, handle)
            handle.flush()
            with self.assertRaisesRegex(audit.RuntimeAuditError, "duplicate critical probe import"):
                audit.load_critical_probe(Path(handle.name))

        report["imports"].pop()
        report["imports"][0]["cumulative_mapped_files"] = ["/opt/conda/./lib.so"]
        with tempfile.NamedTemporaryFile("w", suffix=".json") as handle:
            json.dump(report, handle)
            handle.flush()
            with self.assertRaisesRegex(audit.RuntimeAuditError, "normalized absolute POSIX path"):
                audit.load_critical_probe(Path(handle.name))

if __name__ == "__main__":
    unittest.main()
