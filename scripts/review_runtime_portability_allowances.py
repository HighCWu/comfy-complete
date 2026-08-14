#!/usr/bin/env python3
"""Generate the reviewed exact-fingerprint portability allowance policy.

This is intentionally a small, fail-closed review tool for one audited base
runtime report.  It does not broaden the scanner and it never matches by a
single finding code, a path glob, or a library name alone.  Each candidate
group has an explicit path/needed (or path/interpreter) signature and an
expected count.  A changed report therefore requires a fresh review instead
of silently extending the existing policy.

Example::

    python scripts/review_runtime_portability_allowances.py \
      --report /tmp/runtime-portability-.../audit-output/runtime-portability.json \
      --output ci/runtime-portability-gate.json

Use ``--check-policy`` in CI/review scripts to verify that a checked-in policy
is exactly what the fixed report produces without rewriting it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


AUDIT_VERSION = "runtime-portability/v2"
SCHEMA_VERSION = 2
POLICY_SCHEMA_VERSION = 2
CRITICAL_PROFILE = "base"
POLICY_PROFILE = "comfy-complete-base-bootstrap-2026-08"
EXPECTED_BLOCKER_COUNT = 175
EXPECTED_ALLOWABLE_COUNT = 136
# The candidate review is intentionally tied to this complete report, not just
# to a count or a set of broad category predicates.
EXPECTED_REPORT_SHA256 = "b321ecc622b6f891b0d4931f5020b993b97d3321e0b5c3003b73bf79ed620499"
MISSING_ELF_DETAIL = "DT_NEEDED is absent from selected runtime and launcher inventory"
MISSING_SHEBANG_DETAIL = "absolute interpreter is not provided (missing)"

Finding = Mapping[str, Any]
Predicate = Callable[[Finding], bool]


class AllowanceReviewError(ValueError):
    """Raised when the fixed report or a candidate group is not exact."""


@dataclass(frozen=True)
class AllowanceGroup:
    """One explicitly reviewed exact-signature group."""

    id: str
    expected_count: int
    reason: str
    predicate: Predicate


def _fingerprint(finding: Finding) -> str:
    """Match ``runtime_portability_audit.portability_finding_fingerprint``."""

    encoded = json.dumps(
        dict(finding), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _required_text(finding: Finding, key: str) -> str:
    value = finding.get(key)
    return value if isinstance(value, str) else ""


def _needed(finding: Finding) -> str:
    evidence = finding.get("evidence")
    if not isinstance(evidence, Mapping):
        return ""
    value = evidence.get("needed")
    return value if isinstance(value, str) else ""


def _interpreter(finding: Finding) -> str:
    evidence = finding.get("evidence")
    if not isinstance(evidence, Mapping):
        return ""
    value = evidence.get("interpreter")
    return value if isinstance(value, str) else ""


def _path_needed_pairs(
    path_to_needed: Mapping[str, Sequence[str]],
) -> Predicate:
    """Build an exact predicate from a full path to its reviewed DT_NEEDED set."""

    expected = {path: frozenset(values) for path, values in path_to_needed.items()}

    def matches(finding: Finding) -> bool:
        if (
            _required_text(finding, "code") != "missing_elf_library"
            or _required_text(finding, "severity") != "blocker"
            or _required_text(finding, "detail") != MISSING_ELF_DETAIL
        ):
            return False
        path = _required_text(finding, "path")
        return path in expected and _needed(finding) in expected[path]

    return matches


def _path_interpreter_pairs(path_to_interpreter: Mapping[str, str]) -> Predicate:
    """Build an exact predicate from a full path to its reviewed shebang."""

    def matches(finding: Finding) -> bool:
        if (
            _required_text(finding, "code") != "missing_absolute_shebang"
            or _required_text(finding, "severity") != "blocker"
            or _required_text(finding, "detail") != MISSING_SHEBANG_DETAIL
        ):
            return False
        path = _required_text(finding, "path")
        return path in path_to_interpreter and _interpreter(finding) == path_to_interpreter[path]

    return matches


def _expand_paths(
    roots: Sequence[str], relative_to_needed: Mapping[str, Sequence[str]]
) -> dict[str, tuple[str, ...]]:
    """Expand an explicit set of environment roots into full runtime paths."""

    return {
        f"{root}/{relative_path}": tuple(needed)
        for root in roots
        for relative_path, needed in relative_to_needed.items()
    }


NON_ENTRY_SHEBANGS: dict[str, str] = {
    "app/comfyui/.ce/envs/geometrypack-nodes/.pixi/envs/default/lib/python3.11/cgi.py": "/usr/local/bin/python",
    "app/comfyui/.ce/envs/geometrypack-nodes/.pixi/envs/default/share/qt/3rd_party_licenses/qtwebengine/src/3rdparty/chromium/third_party/devscripts/licensecheck.pl": "/usr/bin/perl",
    "app/comfyui/.ce/envs/geometrypack-nodes/.pixi/envs/default/share/qt/3rd_party_licenses/qtwebengine/src/3rdparty/chromium/third_party/catapult/tracing/third_party/devscripts/licensecheck.pl": "/usr/bin/perl",
    "app/comfyui/.ce/envs/sam3/.pixi/envs/default/lib/python3.11/cgi.py": "/usr/local/bin/python",
    "opt/conda/lib/python3.11/cgi.py": "/usr/local/bin/python",
    "opt/conda/lib/python3.11/site-packages/evalidate/__init__.py": "/usr/bin/python",
}

PY_SIDE_RELATIVE: dict[str, tuple[str, ...]] = {
    "lib/python3.11/site-packages/PySide6/Qt3DAnimation.cpython-311-x86_64-linux-gnu.so": (
        "libQt63DAnimation.so.6",
        "libQt63DCore.so.6",
    ),
    "lib/python3.11/site-packages/PySide6/Qt3DCore.cpython-311-x86_64-linux-gnu.so": (
        "libQt63DCore.so.6",
    ),
    "lib/python3.11/site-packages/PySide6/Qt3DExtras.cpython-311-x86_64-linux-gnu.so": (
        "libQt63DCore.so.6",
        "libQt63DExtras.so.6",
    ),
    "lib/python3.11/site-packages/PySide6/Qt3DInput.cpython-311-x86_64-linux-gnu.so": (
        "libQt63DCore.so.6",
        "libQt63DInput.so.6",
    ),
    "lib/python3.11/site-packages/PySide6/Qt3DLogic.cpython-311-x86_64-linux-gnu.so": (
        "libQt63DCore.so.6",
        "libQt63DLogic.so.6",
    ),
    "lib/python3.11/site-packages/PySide6/Qt3DRender.cpython-311-x86_64-linux-gnu.so": (
        "libQt63DCore.so.6",
        "libQt63DRender.so.6",
    ),
    "lib/python3.11/site-packages/PySide6/QtCharts.cpython-311-x86_64-linux-gnu.so": (
        "libQt6Charts.so.6",
    ),
    "lib/python3.11/site-packages/PySide6/QtDataVisualization.cpython-311-x86_64-linux-gnu.so": (
        "libQt6DataVisualization.so.6",
    ),
    "lib/python3.11/site-packages/PySide6/QtGraphs.cpython-311-x86_64-linux-gnu.so": (
        "libQt6Graphs.so.6",
        "libQt6Quick3D.so.6",
    ),
    "lib/python3.11/site-packages/PySide6/QtGraphsWidgets.cpython-311-x86_64-linux-gnu.so": (
        "libQt6GraphsWidgets.so.6",
    ),
    "lib/python3.11/site-packages/PySide6/QtMultimedia.cpython-311-x86_64-linux-gnu.so": (
        "libQt6Multimedia.so.6",
    ),
    "lib/python3.11/site-packages/PySide6/QtMultimediaWidgets.cpython-311-x86_64-linux-gnu.so": (
        "libQt6MultimediaWidgets.so.6",
    ),
    "lib/python3.11/site-packages/PySide6/QtNetworkAuth.cpython-311-x86_64-linux-gnu.so": (
        "libQt6NetworkAuth.so.6",
    ),
    "lib/python3.11/site-packages/PySide6/QtPdf.cpython-311-x86_64-linux-gnu.so": (
        "libQt6Pdf.so.6",
    ),
    "lib/python3.11/site-packages/PySide6/QtPdfWidgets.cpython-311-x86_64-linux-gnu.so": (
        "libQt6PdfWidgets.so.6",
    ),
    "lib/python3.11/site-packages/PySide6/QtQuick3D.cpython-311-x86_64-linux-gnu.so": (
        "libQt6Quick3D.so.6",
    ),
    "lib/python3.11/site-packages/PySide6/QtScxml.cpython-311-x86_64-linux-gnu.so": (
        "libQt6Scxml.so.6",
    ),
    "lib/python3.11/site-packages/PySide6/QtSensors.cpython-311-x86_64-linux-gnu.so": (
        "libQt6Sensors.so.6",
    ),
    "lib/python3.11/site-packages/PySide6/QtSerialPort.cpython-311-x86_64-linux-gnu.so": (
        "libQt6SerialPort.so.6",
    ),
    "lib/python3.11/site-packages/PySide6/QtSpatialAudio.cpython-311-x86_64-linux-gnu.so": (
        "libQt6Multimedia.so.6",
        "libQt6SpatialAudio.so.6",
    ),
    "lib/python3.11/site-packages/PySide6/QtStateMachine.cpython-311-x86_64-linux-gnu.so": (
        "libQt6StateMachine.so.6",
    ),
    "lib/python3.11/site-packages/PySide6/QtWebEngineCore.cpython-311-x86_64-linux-gnu.so": (
        "libQt6WebEngineCore.so.6",
    ),
    "lib/python3.11/site-packages/PySide6/QtWebEngineQuick.cpython-311-x86_64-linux-gnu.so": (
        "libQt6WebEngineCore.so.6",
        "libQt6WebEngineQuick.so.6",
    ),
    "lib/python3.11/site-packages/PySide6/QtWebEngineWidgets.cpython-311-x86_64-linux-gnu.so": (
        "libQt6WebEngineWidgets.so.6",
    ),
}

BITSANDBYTES_RELATIVE: dict[str, tuple[str, ...]] = {
    "lib/python3.11/site-packages/bitsandbytes/libbitsandbytes_cuda118.so": (
        "libcublas.so.11",
        "libcublasLt.so.11",
        "libcudart.so.11.0",
        "libcusparse.so.11",
    ),
    "lib/python3.11/site-packages/bitsandbytes/libbitsandbytes_cuda130.so": (
        "libcublas.so.13",
        "libcublasLt.so.13",
        "libcudart.so.13",
        "libnvJitLink.so.13",
    ),
    "lib/python3.11/site-packages/bitsandbytes/libbitsandbytes_rocm62.so": (
        "libhipblas.so.2",
        "libhipblaslt.so.0",
        "libhipsparse.so.1",
    ),
    "lib/python3.11/site-packages/bitsandbytes/libbitsandbytes_rocm63.so": (
        "libhipblas.so.2",
        "libhipblaslt.so.0",
        "libhipsparse.so.1",
    ),
    "lib/python3.11/site-packages/bitsandbytes/libbitsandbytes_rocm64.so": (
        "libhipblas.so.2",
        "libhipblaslt.so.0",
        "libhipsparse.so.1",
    ),
    "lib/python3.11/site-packages/bitsandbytes/libbitsandbytes_rocm70.so": (
        "libhipblas.so.3",
        "libhipblaslt.so.1",
        "libhipsparse.so.4",
    ),
    "lib/python3.11/site-packages/bitsandbytes/libbitsandbytes_rocm71.so": (
        "libhipblas.so.3",
        "libhipblaslt.so.1",
        "libhipsparse.so.4",
    ),
    "lib/python3.11/site-packages/bitsandbytes/libbitsandbytes_rocm72.so": (
        "libhipblas.so.3",
        "libhipblaslt.so.1",
        "libhipsparse.so.4",
    ),
    "lib/python3.11/site-packages/bitsandbytes/libbitsandbytes_xpu.so": (
        "libimf.so",
        "libintlc.so.5",
        "libirng.so",
        "libsvml.so",
        "libsycl.so.8",
    ),
}

TORIO_RELATIVE: dict[str, tuple[str, ...]] = {
    "lib/python3.11/site-packages/torio/lib/_torio_ffmpeg4.so": (
        "libavcodec.so.58",
        "libavdevice.so.58",
        "libavfilter.so.7",
        "libavformat.so.58",
        "libavutil.so.56",
    ),
    "lib/python3.11/site-packages/torio/lib/_torio_ffmpeg5.so": (
        "libavcodec.so.59",
        "libavdevice.so.59",
        "libavfilter.so.8",
        "libavformat.so.59",
        "libavutil.so.57",
    ),
    "lib/python3.11/site-packages/torio/lib/libtorio_ffmpeg4.so": (
        "libavcodec.so.58",
        "libavfilter.so.7",
        "libavformat.so.58",
        "libavutil.so.56",
    ),
    "lib/python3.11/site-packages/torio/lib/libtorio_ffmpeg5.so": (
        "libavcodec.so.59",
        "libavfilter.so.8",
        "libavformat.so.59",
        "libavutil.so.57",
    ),
}

ENV_ROOTS = (
    "app/comfyui/.ce/envs/geometrypack-nodes/.pixi/envs/default",
    "app/comfyui/.ce/envs/sam3/.pixi/envs/default",
    "opt/conda",
)
PY_SIDE_PATHS = _expand_paths((ENV_ROOTS[0],), PY_SIDE_RELATIVE)
BITSANDBYTES_PATHS = _expand_paths((ENV_ROOTS[2],), BITSANDBYTES_RELATIVE)
TORIO_PATHS = _expand_paths(ENV_ROOTS, TORIO_RELATIVE)

CUFILE_RELATIVE: dict[str, tuple[str, ...]] = {
    "lib/python3.11/site-packages/nvidia/cufile/lib/libcufile_rdma.so.1": (
        "libibverbs.so.1",
        "libmlx5.so.1",
        "librdmacm.so.1",
    )
}
CUFILE_PATHS = _expand_paths(ENV_ROOTS, CUFILE_RELATIVE)

ROCM_HIP_PATHS: dict[str, tuple[str, ...]] = {
    f"{ENV_ROOTS[0]}/lib/python3.11/site-packages/bpy/lib/libOpenImageDenoise_device_hip.so.2.3.0": (
        "libamdhip64.so.6",
    ),
    f"{ENV_ROOTS[0]}/lib/python3.11/site-packages/comfy_kitchen/backends/hip/_C.cpython-311-x86_64-linux-gnu.so": (
        "libamdhip64.so.7",
    ),
}

CUPY_OPTIONAL_PATHS: dict[str, tuple[str, ...]] = {
    "opt/conda/lib/python3.11/site-packages/cupy_backends/cuda/libs/cudnn.cpython-311-x86_64-linux-gnu.so": (
        "libcudnn.so.8",
    ),
    "opt/conda/lib/python3.11/site-packages/cupy_backends/cuda/libs/cutensor.cpython-311-x86_64-linux-gnu.so": (
        "libcutensor.so.2",
    ),
}


GROUPS: tuple[AllowanceGroup, ...] = (
    AllowanceGroup(
        id="non-entry-shebangs",
        expected_count=6,
        reason=(
            "Exact shebang findings for vendored cgi.py stdlib modules and a Qt "
            "licensecheck.pl copy. These files are metadata/helpers inside optional "
            "environments, not the critical runtime entrypoints (/app/entrypoint.sh, "
            "/app/comfyui/main.py, or /opt/conda/bin/python); a new path is not covered."
        ),
        predicate=_path_interpreter_pairs(NON_ENTRY_SHEBANGS),
    ),
    AllowanceGroup(
        id="geometrypack-pyside-gui-extensions",
        expected_count=32,
        reason=(
            "Exact optional GeometryPack PySide6 Qt GUI-extension bindings. The "
            "reviewed Qt3D/Charts/Graphs/Multimedia/Pdf/Quick3D/Scxml/Sensors/" 
            "SerialPort/SpatialAudio/StateMachine/WebEngine modules are not part of "
            "the server critical import or launcher contract; every path and DT_NEEDED "
            "pair is enumerated."
        ),
        predicate=_path_needed_pairs(PY_SIDE_PATHS),
    ),
    AllowanceGroup(
        id="bitsandbytes-non-target-backends",
        expected_count=31,
        reason=(
            "Exact bitsandbytes CUDA 11.8, CUDA 13, ROCm 6/7, and XPU backend "
            "variants. The selected runtime targets the provided CUDA 12 backend; "
            "these selector alternatives are non-target and each artifact/needed "
            "pair is enumerated."
        ),
        predicate=_path_needed_pairs(BITSANDBYTES_PATHS),
    ),
    AllowanceGroup(
        id="cufile-rdma-plugin",
        expected_count=9,
        reason=(
            "Exact optional NVIDIA cuFile RDMA plugin dependencies in the two "
            "isolated environments and base environment. The target runtime uses "
            "the non-RDMA cuFile library; RDMA fabric libraries are not selected, "
            "and only the enumerated plugin findings are allowed."
        ),
        predicate=_path_needed_pairs(CUFILE_PATHS),
    ),
    AllowanceGroup(
        id="rocm-hip-backends",
        expected_count=2,
        reason=(
            "Exact optional ROCm/HIP backend objects from GeometryPack. The target "
            "runtime is the NVIDIA profile and does not select HIP; only these two "
            "enumerated backend DT_NEEDED findings are reviewed."
        ),
        predicate=_path_needed_pairs(ROCM_HIP_PATHS),
    ),
    AllowanceGroup(
        id="cupy-optional-cuda-libraries",
        expected_count=2,
        reason=(
            "Exact optional CuPy cudnn8 and cutensor2 bindings. They are selector-" 
            "optional compatibility libraries rather than the target CUDA runtime "
            "closure; no other CuPy or CUDA finding is covered."
        ),
        predicate=_path_needed_pairs(CUPY_OPTIONAL_PATHS),
    ),
    AllowanceGroup(
        id="torio-ffmpeg4-5-old-abi",
        expected_count=54,
        reason=(
            "Exact torio FFmpeg4 and FFmpeg5 compatibility ABI objects across the "
            "two isolated selector environments and base environment. These are "
            "non-target optional/legacy variants; FFmpeg6 findings remain blockers "
            "until the enhanced slim launcher inventory is reviewed."
        ),
        predicate=_path_needed_pairs(TORIO_PATHS),
    ),
)


def _load_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AllowanceReviewError(f"cannot read JSON {path}: {error}") from error


def _load_fixed_report(path: Path) -> object:
    try:
        raw = path.read_bytes()
    except (OSError,):
        raise AllowanceReviewError(f"cannot read fixed report {path}") from None
    digest = hashlib.sha256(raw).hexdigest()
    if digest != EXPECTED_REPORT_SHA256:
        raise AllowanceReviewError(
            f"report SHA-256 changed: expected {EXPECTED_REPORT_SHA256}, got {digest}"
        )
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AllowanceReviewError(f"fixed report JSON is invalid: {error}") from error


def _validate_fixed_report(report: object) -> list[dict[str, Any]]:
    if not isinstance(report, dict):
        raise AllowanceReviewError("runtime portability report must be an object")
    if report.get("schema_version") != SCHEMA_VERSION:
        raise AllowanceReviewError("report schema_version is not the fixed v2 report")
    if report.get("audit_version") != AUDIT_VERSION:
        raise AllowanceReviewError("report audit_version does not match runtime-portability/v2")
    if report.get("status") != "blocker":
        raise AllowanceReviewError("report status must remain blocker for this review")

    summary = report.get("summary")
    if not isinstance(summary, Mapping):
        raise AllowanceReviewError("report summary is missing")
    finding_counts = summary.get("finding_counts")
    if not isinstance(finding_counts, Mapping) or finding_counts.get("blocker") != EXPECTED_BLOCKER_COUNT:
        raise AllowanceReviewError("report summary blocker count is not the fixed 175")

    gate = report.get("gate")
    if not isinstance(gate, Mapping):
        raise AllowanceReviewError("report gate is missing")
    if gate.get("source") != "critical" or gate.get("profile") != CRITICAL_PROFILE or gate.get("probe_profile") != "cpu":
        raise AllowanceReviewError("report critical gate contract changed")
    static_policy = gate.get("static_policy")
    if not isinstance(static_policy, Mapping):
        raise AllowanceReviewError("report static portability gate is missing")
    if static_policy.get("profile") != POLICY_PROFILE:
        raise AllowanceReviewError("report static policy profile changed")
    if static_policy.get("raw_blocker_count") != EXPECTED_BLOCKER_COUNT:
        raise AllowanceReviewError("report static raw blocker count is not the fixed 175")
    if static_policy.get("allowed_blocker_count") != 0 or static_policy.get("unapproved_blocker_count") != EXPECTED_BLOCKER_COUNT:
        raise AllowanceReviewError("fixed report must be generated with no existing allowances")

    raw_findings = report.get("findings")
    if not isinstance(raw_findings, list):
        raise AllowanceReviewError("report findings must be an array")
    blockers = [item for item in raw_findings if isinstance(item, dict) and item.get("severity") == "blocker"]
    if len(blockers) != EXPECTED_BLOCKER_COUNT:
        raise AllowanceReviewError("report findings do not contain exactly 175 blockers")
    if len({_fingerprint(item) for item in blockers}) != EXPECTED_BLOCKER_COUNT:
        raise AllowanceReviewError("blocker fingerprints are not unique; exact allowance is unsafe")

    gate_findings = static_policy.get("unapproved_findings")
    if not isinstance(gate_findings, list) or len(gate_findings) != EXPECTED_BLOCKER_COUNT:
        raise AllowanceReviewError("report static unapproved findings are not the fixed 175")
    expected_fingerprints = {_fingerprint(item) for item in blockers}
    reported_fingerprints = {
        item.get("finding_fingerprint")
        for item in gate_findings
        if isinstance(item, Mapping)
    }
    if reported_fingerprints != expected_fingerprints:
        raise AllowanceReviewError("report static finding fingerprints do not match raw findings")

    return blockers


def review_candidates(blockers: Sequence[Finding]) -> dict[str, list[dict[str, Any]]]:
    """Return exact candidate findings after all fail-closed group checks."""

    selected: dict[str, list[dict[str, Any]]] = {group.id: [] for group in GROUPS}
    fingerprints: dict[str, str] = {}
    for finding in blockers:
        matches = [group for group in GROUPS if group.predicate(finding)]
        if len(matches) > 1:
            raise AllowanceReviewError(
                f"finding matches multiple allowance groups: {_fingerprint(finding)}"
            )
        if not matches:
            continue
        group = matches[0]
        fingerprint = _fingerprint(finding)
        if fingerprint in fingerprints:
            raise AllowanceReviewError(
                f"finding fingerprint appears in more than one candidate: {fingerprint}"
            )
        fingerprints[fingerprint] = group.id
        selected[group.id].append(dict(finding))

    for group in GROUPS:
        findings = selected[group.id]
        if len(findings) != group.expected_count:
            raise AllowanceReviewError(
                f"allowance group {group.id} matched {len(findings)} findings; "
                f"expected {group.expected_count}"
            )
    if sum(len(findings) for findings in selected.values()) != EXPECTED_ALLOWABLE_COUNT:
        raise AllowanceReviewError("candidate total changed from the fixed 136 findings")
    return selected


def build_policy(report: object) -> dict[str, Any]:
    """Validate the fixed report and build deterministic policy JSON."""

    blockers = _validate_fixed_report(report)
    selected = review_candidates(blockers)
    allowances = []
    for group in GROUPS:
        fingerprints = sorted(_fingerprint(finding) for finding in selected[group.id])
        if len(fingerprints) != len(set(fingerprints)):
            raise AllowanceReviewError(f"allowance group {group.id} has duplicate fingerprints")
        allowances.append(
            {
                "id": group.id,
                "finding_fingerprints": fingerprints,
                "max_matches": len(fingerprints),
                "reason": group.reason,
            }
        )
    return {
        "schema_version": POLICY_SCHEMA_VERSION,
        "audit_version": AUDIT_VERSION,
        "critical_profile": CRITICAL_PROFILE,
        "profile": POLICY_PROFILE,
        "default_action": "block",
        "allowances": allowances,
    }


def _render(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--check-policy",
        type=Path,
        help="check an existing policy against the generated exact policy",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = _load_fixed_report(args.report)
        policy = build_policy(report)
        rendered = _render(policy)
        if args.check_policy is not None:
            existing = _load_json(args.check_policy)
            if existing != policy:
                raise AllowanceReviewError(
                    f"checked policy does not match exact candidates: {args.check_policy}"
                )
        if args.output is not None:
            args.output.write_text(rendered, encoding="utf-8")
        else:
            sys.stdout.write(rendered)
        return 0
    except (AllowanceReviewError, OSError) as error:
        print(f"runtime portability allowance review failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
