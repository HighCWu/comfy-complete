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


def test_automatic_staging_dispatch_is_explicitly_opt_in():
    text = DOCKER_BUILD.read_text()
    assert "vars.AUTO_STAGING_ROLLOUT == 'true'" in text
    assert "LAIMON_REPOSITORY_DISPATCH_TOKEN" in text
    assert '"event_type":"comfy-image-built"' in text


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
