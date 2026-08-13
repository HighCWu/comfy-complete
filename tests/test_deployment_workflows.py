"""Safety contracts for image build and deployment workflows."""

from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
DOCKER_BUILD = REPO_ROOT / ".github" / "workflows" / "docker-build.yml"


def test_docker_build_never_mutates_runpod_endpoints():
    """A push to main may publish images, but must not deploy an endpoint."""
    text = DOCKER_BUILD.read_text()
    assert "api.runpod.io/v2/serverless" not in text
    assert "Update RunPod endpoint" not in text
    assert "RUNPOD_ENDPOINT_ID" not in text


def test_docker_build_does_not_need_cross_repository_credentials():
    text = DOCKER_BUILD.read_text()
    assert "repository_dispatch" not in text
    assert "LAIMON_REPOSITORY_DISPATCH_TOKEN" not in text


def test_base_content_hash_covers_every_local_copy_input():
    """Changing a file copied into the base must invalidate its reusable tag."""
    workflow = DOCKER_BUILD.read_text()
    dockerfile = (REPO_ROOT / "docker" / "Dockerfile.cloudbuild").read_text()
    local_copy_inputs = {
        line.split()[1]
        for line in dockerfile.splitlines()
        if line.startswith("COPY ") and "--from=" not in line
    }
    for path in local_copy_inputs:
        assert path in workflow, f"base content hash is missing Docker COPY input: {path}"


def test_docker_workflow_yaml_is_valid():
    parsed = yaml.safe_load(DOCKER_BUILD.read_text())
    assert isinstance(parsed, dict)
    assert "jobs" in parsed


def test_sageattn_checks_manifest_before_expensive_setup():
    workflow = DOCKER_BUILD.read_text()
    block = workflow.split("  build-sageattn:\n", 1)[1].split("\n  build-base:", 1)[0]
    check = block.index("Check if sageattn image already exists")
    cleanup = block.index("Free disk space and move Docker data to /mnt")
    checkout = block.index("actions/checkout@v7")
    assert check < cleanup < checkout
    setup = block[cleanup:]
    assert "if: steps.check.outputs.skipped != 'true'" in setup


def test_runtime_audit_job_reuses_published_base_and_is_read_only():
    workflow = DOCKER_BUILD.read_text()
    parsed = yaml.safe_load(workflow)
    job = parsed["jobs"]["audit-base-runtime"]

    assert job["needs"] == "build-base"
    assert job["permissions"] == {"contents": "read", "packages": "read"}
    assert "packages: write" not in workflow.split("audit-base-runtime:", 1)[1]


def test_runtime_audit_job_materializes_before_auditing_and_uploads_on_failure():
    workflow = DOCKER_BUILD.read_text()
    block = workflow.split("  audit-base-runtime:\n", 1)[1]

    pull = block.index("docker pull")
    run = block.index("docker run --rm")
    audit = block.index("scripts/runtime_portability_audit.py")
    upload = block.index("actions/upload-artifact@v4")
    cleanup = block.index("Remove temporary audit files")
    assert pull < run < audit < upload < cleanup
    assert "set -euo pipefail" in block
    assert "2>&1 | tee" in block
    assert "if: ${{ always() }}" in block
    assert "--source-root /" in block
    assert "--max-entries 500000" in block
    assert "--max-findings 50000" in block
    assert "--read-only" in block
    assert "--pids-limit 256" in block
    assert "--network none" in block
    assert "runtime_portability_audit.py:ro" in block
    assert "-v \"$PWD/ci:/runtime-audit/ci:ro\"" in block
    assert "--source-root ." not in block
    assert "docker export" not in block
    assert "runtime.tar" not in block
    assert ".tar.zst" not in block


def test_runtime_audit_has_critical_probe_and_uploads_its_evidence():
    workflow = DOCKER_BUILD.read_text()
    block = workflow.split("  audit-base-runtime:\n", 1)[1]
    assert "runtime-critical-entrypoints.json" in block
    assert "runtime_critical_probe.py" in block
    assert "critical-probe.json" in block
    assert "critical-probe.log" in block
    assert "--critical-config" in block
    assert "--critical-probe" in block
    assert "--network none" in block
    assert "--read-only" in block
    assert "--cap-drop ALL" in block
    assert block.count("--pids-limit 256") >= 2
    assert "--security-opt no-new-privileges" in block
    assert "CUDA_VISIBLE_DEVICES=" in block
    assert "writable_paths" in block
    assert "if: ${{ always() }}" in block


def test_runtime_audit_uses_explicit_empty_inventory_without_claiming_success():
    inventory = REPO_ROOT / "ci" / "runtime-launcher-inventory.json"
    assert yaml.safe_load(inventory.read_text()) == {
        "system_paths": [],
        "libraries": [],
        "library_paths": [],
        "symlinks": {},
    }

    policy = yaml.safe_load((REPO_ROOT / "ci" / "runtime-selection-policy.json").read_text())
    assert policy["targets"] == ["/app/comfyui", "/opt/conda"]
    assert policy["include_app"] == []
