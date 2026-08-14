"""Safety contracts for image build and deployment workflows."""

import json
import os
import shlex
import subprocess
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
DOCKER_BUILD = REPO_ROOT / ".github" / "workflows" / "docker-build.yml"
DOCKER_PULL_RETRY = REPO_ROOT / ".github" / "scripts" / "docker-pull-with-retry.sh"
CONFIGURE_DOCKER_PULLS = REPO_ROOT / ".github" / "scripts" / "configure-docker-pulls.py"


def _job_block(workflow: str, job: str, next_job: str) -> str:
    return workflow.split(f"  {job}:\n", 1)[1].split(f"\n  {next_job}:", 1)[0]


def _local_copy_inputs(dockerfile: str) -> list[str]:
    """Return local COPY sources, excluding flags and the destination."""
    inputs: list[str] = []
    for line in dockerfile.splitlines():
        stripped = line.strip()
        if not stripped.startswith("COPY ") or "--from=" in stripped:
            continue
        tokens = shlex.split(stripped)
        sources = [token for token in tokens[1:] if not token.startswith("--")]
        inputs.extend(sources[:-1])
    return inputs


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


def test_wrapper_content_hashes_cover_every_local_copy_input_and_base_identity():
    """Wrapper tags must change with the immutable base or any local COPY input."""
    workflow = DOCKER_BUILD.read_text()
    cases = (
        ("build-runpod", "build-pod", "docker/Dockerfile.runpod", "runpod-"),
        ("build-pod", "build-hydrator", "docker/Dockerfile.pod", "pod-"),
    )
    for job, next_job, dockerfile_path, tag_prefix in cases:
        block = _job_block(workflow, job, next_job)
        hash_step = block.split("Calculate ", 1)[1].split("\n\n      - uses: docker/login-action", 1)[0]
        assert "${{ needs.build-base.outputs.base_tag }}" in hash_step
        assert dockerfile_path in hash_step
        for path in _local_copy_inputs((REPO_ROOT / dockerfile_path).read_text()):
            assert path in hash_step, f"{job} wrapper hash is missing Docker COPY input: {path}"
        assert f'echo "tag={tag_prefix}${{HASH:0:16}}"' in hash_step
        assert "--build-arg BASE_IMAGE=${{ steps.img.outputs.base }}:${{ needs.build-base.outputs.base_tag }}" in block
        assert "--build-arg BASE_IMAGE=${{ steps.img.outputs.base }}:latest" not in block


def test_wrapper_manifest_checks_precede_expensive_setup_and_hits_retag_immutable_content():
    workflow = DOCKER_BUILD.read_text()
    cases = (
        ("build-runpod", "build-pod", "runpod"),
        ("build-pod", "build-hydrator", "pod"),
    )
    for job, next_job, image_suffix in cases:
        block = _job_block(workflow, job, next_job)
        checkout = block.index("actions/checkout@v7")
        calculate = block.index("Calculate ")
        login = block.index("docker/login-action@v4")
        manifest = block.index("docker manifest inspect")
        cleanup = block.index("Free disk space and move Docker data to /mnt")
        assert checkout < calculate < login < manifest < cleanup
        expensive = block[cleanup:]
        assert "if: steps.existing.outputs.reuse != 'true'" in expensive

        assert f'SOURCE="${{{{ steps.img.outputs.base }}}}-{image_suffix}:${{{{ steps.wrapper.outputs.tag }}}}"' in block
        assert 'crane tag "$SOURCE" "${{ github.sha }}"' in block
        assert 'crane tag "$SOURCE" latest' in block
        assert "if: steps.existing.outputs.reuse == 'true'" in block


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
    audit_block = workflow.split("  audit-base-runtime:\n", 1)[1].split("\n  publish-runtime-slim:", 1)[0]
    assert "packages: write" not in audit_block


def test_large_base_pulls_limit_request_bursts_and_use_bounded_retry():
    workflow = DOCKER_BUILD.read_text()
    blocks = (
        _job_block(workflow, "audit-base-runtime", "publish-runtime-slim"),
        workflow.split("  publish-runtime-slim:\n", 1)[1],
    )
    for block in blocks:
        configure = block.index("configure-docker-pulls.py")
        restart = block.index("systemctl restart docker", configure)
        pull = block.index("docker-pull-with-retry.sh")
        assert configure < restart < pull
        assert "docker pull \"${{" not in block

    retry_script = DOCKER_PULL_RETRY.read_text()
    assert 'DOCKER_PULL_RETRY_ATTEMPTS:-5' in retry_script
    assert "attempt <= max_attempts" in retry_script
    assert "base_delay * (1 << (attempt - 1))" in retry_script
    assert "toomanyrequests" in retry_script


def test_docker_pull_daemon_config_merges_existing_keys(tmp_path: Path):
    config_path = tmp_path / "daemon.json"
    config_path.write_text(json.dumps({"data-root": "/mnt/docker", "debug": True}))

    subprocess.run(
        ["python3", str(CONFIGURE_DOCKER_PULLS), str(config_path)],
        check=True,
    )

    assert json.loads(config_path.read_text()) == {
        "data-root": "/mnt/docker",
        "debug": True,
        "max-concurrent-downloads": 1,
        "max-download-attempts": 5,
    }


def _write_fake_pull_commands(tmp_path: Path) -> dict[str, str]:
    docker = tmp_path / "docker"
    docker.write_text(
        """#!/usr/bin/env bash
count=0
if [ -f "$FAKE_DOCKER_COUNT" ]; then count="$(cat "$FAKE_DOCKER_COUNT")"; fi
count=$((count + 1))
printf '%s' "$count" > "$FAKE_DOCKER_COUNT"
if [ "$FAKE_DOCKER_MODE" = transient-then-success ] && [ "$count" -ge 3 ]; then
  echo 'pull complete'
  exit 0
fi
if [ "$FAKE_DOCKER_MODE" = permanent ]; then
  echo 'unauthorized: authentication required' >&2
else
  echo 'toomanyrequests: retry-after: 265ms' >&2
fi
exit 1
"""
    )
    sleep = tmp_path / "sleep"
    sleep.write_text("#!/usr/bin/env bash\necho \"$1\" >> \"$FAKE_SLEEP_LOG\"\n")
    docker.chmod(0o755)
    sleep.chmod(0o755)
    return {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "FAKE_DOCKER_COUNT": str(tmp_path / "count"),
        "FAKE_SLEEP_LOG": str(tmp_path / "sleeps"),
        "DOCKER_PULL_RETRY_BASE_SECONDS": "2",
        "DOCKER_PULL_RETRY_JITTER_SECONDS": "0",
    }


def test_docker_pull_retry_recovers_with_increasing_bounded_backoff(tmp_path: Path):
    env = _write_fake_pull_commands(tmp_path)
    env["FAKE_DOCKER_MODE"] = "transient-then-success"

    result = subprocess.run(
        ["bash", str(DOCKER_PULL_RETRY), "ghcr.io/example/image:immutable"],
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "count").read_text() == "3"
    assert (tmp_path / "sleeps").read_text().splitlines() == ["2", "4"]


def test_docker_pull_retry_fails_permanent_errors_immediately(tmp_path: Path):
    env = _write_fake_pull_commands(tmp_path)
    env["FAKE_DOCKER_MODE"] = "permanent"

    result = subprocess.run(
        ["bash", str(DOCKER_PULL_RETRY), "ghcr.io/example/missing:immutable"],
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert (tmp_path / "count").read_text() == "1"
    assert not (tmp_path / "sleeps").exists()
    assert "non-transient error" in result.stderr


def test_docker_pull_retry_stops_after_five_transient_failures(tmp_path: Path):
    env = _write_fake_pull_commands(tmp_path)
    env["FAKE_DOCKER_MODE"] = "always-transient"
    env["DOCKER_PULL_RETRY_BASE_SECONDS"] = "0"

    result = subprocess.run(
        ["bash", str(DOCKER_PULL_RETRY), "ghcr.io/example/image:immutable"],
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert (tmp_path / "count").read_text() == "5"
    assert (tmp_path / "sleeps").read_text().splitlines() == ["0", "0", "0", "0"]
    assert "exhausted 5 attempts" in result.stderr


def test_runtime_publication_waits_for_audit_and_keeps_large_archive_off_artifacts():
    workflow = DOCKER_BUILD.read_text()
    parsed = yaml.safe_load(workflow)
    job = parsed["jobs"]["publish-runtime-slim"]

    assert job["needs"] == ["build-base", "audit-base-runtime"]
    assert job["permissions"] == {"contents": "read", "packages": "write"}
    assert job["timeout-minutes"] == 360
    block = workflow.split("  publish-runtime-slim:\n", 1)[1]
    assert "scripts/export_runtime.py" in block
    assert "scripts/runtime_ready.py" in block
    assert "actions/download-artifact@v4" in block
    assert 'gate.get("status") != "pass"' in block
    assert 'gate.get("source") != "critical"' in block
    assert "docker/Dockerfile.pod-slim" in block
    assert "--network none" in block
    assert "--cap-drop ALL" in block
    assert "--cap-add DAC_READ_SEARCH" in block
    assert "--security-opt no-new-privileges" in block
    assert 'sudo install -d -o 0 -g "$(id -g)" -m 0770 /mnt/runtime-export' in block
    assert "--cap-add DAC_OVERRIDE" not in block
    assert "runtime-summary.json" in block
    assert "slim-image.json" in block
    upload = block.split("actions/upload-artifact@v4", 1)[1].split("Delete the runner-local runtime archive", 1)[0]
    assert ".tar.zst" not in upload
    assert "docker push" in block
    assert "-pod-slim:${{ github.sha }}" in block
    assert "-pod-slim:latest" not in block


def test_runtime_audit_job_materializes_before_auditing_and_uploads_on_failure():
    workflow = DOCKER_BUILD.read_text()
    block = workflow.split("  audit-base-runtime:\n", 1)[1].split("\n  publish-runtime-slim:", 1)[0]

    pull = block.index("docker-pull-with-retry.sh")
    create = block.index("docker create")
    start = block.index("docker start -a")
    audit = block.index("--source-root /")
    upload = block.index("actions/upload-artifact@v4")
    cleanup = block.index("Remove temporary audit files")
    assert pull < create < start < audit < upload < cleanup
    assert "set -euo pipefail" in block
    assert "2>&1 | tee" in block
    assert "if: ${{ always() }}" in block
    assert "--source-root /" in block
    assert "--max-entries 500000" in block
    assert "--max-findings 50000" in block
    assert "--read-only" in block
    assert "--pids-limit 256" in block
    assert "--network none" in block
    assert "--mount type=volume,source=\"$PROBE_VOLUME\",destination=/audit" in block
    assert "--mount type=volume,source=\"$PROBE_VOLUME\",destination=/audit-input,readonly" in block
    assert "--mount type=volume,source=\"$AUDIT_VOLUME\",destination=/audit-output" in block
    assert "-v \"$PROBE_VOLUME:/evidence:ro\"" in block
    assert "-v \"$AUDIT_VOLUME:/evidence:ro\"" in block
    assert "CAT_BIN=/bin/cat" in block
    assert "test -x /bin/cat" in block
    assert "test -x /usr/bin/cat" in block
    assert "CAT_BIN=/usr/bin/cat" in block
    assert "--entrypoint \"$CAT_BIN\"" in block
    assert "--entrypoint /bin/sh" in block
    assert "-v \"$PROBE_DIR:/input:ro\"" in block
    assert "> \"$PROBE_DIR/critical-probe.json\"" in block
    assert "> \"$AUDIT_OUTPUT_DIR/runtime-portability.json\"" in block
    assert "docker rm -f \"$PROBE_CONTAINER\"" in block
    assert "docker rm -f \"$AUDIT_CONTAINER\"" in block
    assert "--output /audit/critical-probe.json" in block
    assert "--output /audit-output/runtime-portability.json" in block
    assert "docker volume create \"$PROBE_VOLUME\"" in block
    assert "docker volume create \"$AUDIT_VOLUME\"" in block
    assert "docker run --rm" in block
    assert "docker cp" not in block
    assert ':/audit:rw"' not in block
    assert ':/audit-output:rw"' not in block
    assert ':/audit-input:rw"' not in block
    assert not any(' -v "$' in line and ':rw"' in line for line in block.splitlines())
    assert "sudo chown" not in block
    assert "sudo chmod" not in block
    assert "reclaim_probe_output" not in block
    assert "reclaim_audit_output" not in block
    assert "-v \"$PROBE_DIR:/audit-input:ro\"" not in block
    assert "-v \"$AUDIT_OUTPUT_DIR:/audit-output:rw\"" not in block
    assert "chmod 0777" not in block
    assert "--user root" not in block
    assert "runtime_portability_audit.py:ro" in block
    assert "-v \"$PWD/ci:/runtime-audit/ci:ro\"" in block
    assert "--source-root ." not in block
    assert "docker export" not in block
    assert "runtime.tar" not in block
    assert ".tar.zst" not in block


def test_runtime_audit_uses_named_volumes_for_container_artifacts():
    workflow = DOCKER_BUILD.read_text()
    block = workflow.split("  audit-base-runtime:\n", 1)[1].split("\n  publish-runtime-slim:", 1)[0]

    assert "--output /audit/critical-probe.json" in block
    assert "--output /audit-output/runtime-portability.json" in block
    assert "--critical-probe /audit-input/critical-probe.json" in block
    assert "-v \"$PROBE_DIR:/audit-input:ro\"" not in block
    assert "-v \"$AUDIT_OUTPUT_DIR:/audit-output:rw\"" not in block
    assert "-v \"$PROBE_VOLUME:/evidence:ro\"" in block
    assert "-v \"$AUDIT_VOLUME:/evidence:ro\"" in block
    assert "CAT_BIN=/bin/cat" in block
    assert "test -x /bin/cat" in block
    assert "test -x /usr/bin/cat" in block
    assert "CAT_BIN=/usr/bin/cat" in block
    assert "--entrypoint \"$CAT_BIN\"" in block
    assert "--entrypoint /bin/sh" in block
    assert "-v \"$PROBE_DIR:/input:ro\"" in block
    assert "> \"$PROBE_DIR/critical-probe.json\"" in block
    assert "> \"$AUDIT_OUTPUT_DIR/runtime-portability.json\"" in block
    assert ':/audit:rw"' not in block
    assert ':/audit-output:rw"' not in block
    assert ':/audit-input:rw"' not in block
    assert not any(' -v "$' in line and ':rw"' in line for line in block.splitlines())


def test_runtime_audit_has_critical_probe_and_uploads_its_evidence():
    workflow = DOCKER_BUILD.read_text()
    block = workflow.split("  audit-base-runtime:\n", 1)[1].split("\n  publish-runtime-slim:", 1)[0]
    assert "runtime-critical-entrypoints.json" in block
    assert "runtime_critical_probe.py" in block
    assert "critical-probe.json" in block
    assert "critical-probe.log" in block
    assert "--critical-config" in block
    assert "--critical-probe" in block
    assert "--profile cpu" in block
    assert "--critical-profile cpu" in block
    assert '"probe_profile": "cpu"' in block
    assert '"coverage": "incomplete"' in block
    assert "--network none" in block
    assert "--read-only" in block
    assert "--cap-drop ALL" in block
    assert block.count("--pids-limit 256") >= 2
    assert "--security-opt no-new-privileges" in block
    assert "CUDA_VISIBLE_DEVICES=" in block
    assert "writable_paths" in block
    assert "if: ${{ always() }}" in block


def test_runtime_audit_launcher_inventory_is_the_evidence_backed_critical_closure():
    inventory = REPO_ROOT / "ci" / "runtime-launcher-inventory.json"
    assert yaml.safe_load(inventory.read_text()) == {
        "system_paths": [
            "/bin/bash",
            "/usr/bin/dumb-init",
            "/usr/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2",
        ],
        "libraries": ["libc.so.6", "libdl.so.2", "libm.so.6", "libpthread.so.0", "librt.so.1", "libutil.so.1"],
        "library_paths": [],
        "executable_paths": [
            "/bin/bash",
            "/usr/bin/dumb-init",
            "/usr/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2",
        ],
        "symlinks": {
            "/lib64/ld-linux-x86-64.so.2": "/usr/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2",
        },
    }

    policy = yaml.safe_load((REPO_ROOT / "ci" / "runtime-selection-policy.json").read_text())
    assert policy["targets"] == ["/app/comfyui", "/opt/conda"]
    assert policy["include_app"] == []
    assert policy["excludes"] == [
        "/opt/conda/pkgs",
        "/app/comfyui/models/_xdgcache",
        "/app/comfyui/models/_xdgconfig",
        "/app/comfyui/models/_xdgdata",
        "/app/comfyui/output",
        "/app/comfyui/temp",
    ]
