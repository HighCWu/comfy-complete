"""Tests for the fixed-report exact-fingerprint allowance review."""

from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "review_runtime_portability_allowances",
    ROOT / "scripts" / "review_runtime_portability_allowances.py",
)
assert SPEC and SPEC.loader
review = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = review
SPEC.loader.exec_module(review)


class RuntimePortabilityAllowanceReviewTests(unittest.TestCase):
    def finding(self, *, path: str, needed: str) -> dict[str, object]:
        return {
            "code": "missing_elf_library",
            "detail": review.MISSING_ELF_DETAIL,
            "evidence": {"needed": needed},
            "path": path,
            "severity": "blocker",
        }

    def test_reviewed_groups_are_explicit_and_sum_to_expected_allowances(self) -> None:
        self.assertEqual(sum(group.expected_count for group in review.GROUPS), 136)
        self.assertEqual(review.EXPECTED_ALLOWABLE_COUNT, 136)
        self.assertEqual(len({group.id for group in review.GROUPS}), len(review.GROUPS))
        self.assertTrue(all(group.expected_count > 0 for group in review.GROUPS))

    def test_non_target_names_are_not_covered_by_single_code_or_library_rules(self) -> None:
        findings = [
            self.finding(
                path=(
                    "app/comfyui/.ce/envs/geometrypack-nodes/.pixi/envs/default/"
                    "lib/python3.11/site-packages/torio/lib/_torio_ffmpeg6.so"
                ),
                needed="libavcodec.so.60",
            ),
            self.finding(
                path="opt/conda/lib/python3.11/site-packages/_soundfile_data/libsndfile_x86_64.so",
                needed="libmvec.so.1",
            ),
            self.finding(
                path=(
                    "app/comfyui/.ce/envs/geometrypack-nodes/.pixi/envs/default/"
                    "lib/python3.11/site-packages/bpy/lib/libMaterialXRenderGlsl.so.1.38.8"
                ),
                needed="libXt.so.6",
            ),
            self.finding(
                path="opt/conda/lib/python3.11/site-packages/pymeshlab/lib/libgcrypt.so.20",
                needed="libgpg-error.so.0",
            ),
            self.finding(
                path=(
                    "opt/conda/lib/python3.11/site-packages/torchaudio/lib/"
                    "libtorchaudio_sox.so"
                ),
                needed="libsox.so",
            ),
        ]
        for finding in findings:
            self.assertEqual(
                [group.id for group in review.GROUPS if group.predicate(finding)],
                [],
            )

    def test_changed_path_or_needed_value_is_fail_closed(self) -> None:
        original_path = next(iter(review.PY_SIDE_PATHS))
        original_needed = review.PY_SIDE_PATHS[original_path][0]
        changed_path = original_path.replace("PySide6", "PySide7")
        changed = self.finding(path=changed_path, needed=original_needed)
        self.assertFalse(any(group.predicate(changed) for group in review.GROUPS))

        changed_needed = self.finding(path=original_path, needed="libQt6Widgets.so.6")
        self.assertFalse(any(group.predicate(changed_needed) for group in review.GROUPS))

    def test_policy_file_has_sorted_unique_exact_fingerprints_and_counts(self) -> None:
        policy_path = ROOT / "ci" / "runtime-portability-gate.json"
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        self.assertEqual(policy["schema_version"], review.POLICY_SCHEMA_VERSION)
        self.assertEqual(policy["audit_version"], review.AUDIT_VERSION)
        self.assertEqual(policy["default_action"], "block")
        self.assertEqual(len(policy["allowances"]), len(review.GROUPS))
        total = 0
        for allowance in policy["allowances"]:
            fingerprints = allowance["finding_fingerprints"]
            self.assertEqual(fingerprints, sorted(set(fingerprints)))
            self.assertEqual(allowance["max_matches"], len(fingerprints))
            self.assertTrue(allowance["reason"])
            total += len(fingerprints)
        self.assertEqual(total, review.EXPECTED_ALLOWABLE_COUNT)

    def test_fixed_report_contract_rejects_missing_or_changed_metadata(self) -> None:
        with self.assertRaises(review.AllowanceReviewError):
            review.build_policy({})

        changed = {
            "schema_version": review.SCHEMA_VERSION,
            "audit_version": review.AUDIT_VERSION,
            "status": "pass",
        }
        with self.assertRaises(review.AllowanceReviewError):
            review.build_policy(changed)

    def test_real_report_generates_checked_in_policy_when_available(self) -> None:
        report_path = Path(
            "/tmp/runtime-portability-31806909410.GwJNpW/audit-output/runtime-portability.json"
        )
        if not report_path.exists():
            self.skipTest("the fixed audit report is not available in this checkout")
        report = review._load_fixed_report(report_path)
        generated = review.build_policy(report)
        checked_in = json.loads(
            (ROOT / "ci" / "runtime-portability-gate.json").read_text(encoding="utf-8")
        )
        self.assertEqual(generated, checked_in)


if __name__ == "__main__":
    unittest.main()
