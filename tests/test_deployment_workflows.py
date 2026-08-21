"""Safety contracts for image build and deployment workflows."""

import json
import os
import shlex
import subprocess
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
DOCKER_BUILD = REPO_ROOT / ".github" / "workflows" / "docker-build.yml"
SEED_OBJECT_INFO = REPO_ROOT / ".github" / "workflows" / "seed-object-info.yml"
RUNTIME_PUBLICATION_PREFLIGHT = (
    REPO_ROOT / ".github" / "workflows" / "runtime-publication-preflight.yml"
)
RUNTIME_PUBLISHER_REQUIREMENTS = REPO_ROOT / "scripts" / "runtime-publisher-requirements.txt"
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
    assert "PROJECT_REPOSITORY_DISPATCH_TOKEN" not in text


def test_optional_object_info_integration_skips_only_when_fully_unconfigured():
    text = SEED_OBJECT_INFO.read_text()
    block = text.split("      - name: Check optional API integration\n", 1)[1]
    block = block.split("      - name: Resolve image tag\n", 1)[0]
    assert '[ -z "$ADMIN_SECRET" ] && [ -z "$API_BASE_URL" ]' in block
    assert '[ -z "$ADMIN_SECRET" ] || [ -z "$API_BASE_URL" ]' in block
    assert "must be configured together" in block
    assert "exit 1" in block


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


def test_runtime_materializer_image_is_small_secretless_and_content_tagged():
    workflow = DOCKER_BUILD.read_text()
    dockerfile = (REPO_ROOT / "docker" / "Dockerfile.runtime-materializer").read_text()
    parsed = yaml.safe_load(workflow)
    job = parsed["jobs"]["build-runtime-materializer"]
    assert job["permissions"] == {"contents": "read", "packages": "write"}
    block = workflow.split("  build-runtime-materializer:\n", 1)[1].split("\n  build-base:", 1)[0]

    for path in (
        "docker/Dockerfile.runtime-materializer",
        "scripts/download_materialize_runtime.py",
        "scripts/materialize_runtime.py",
        "scripts/runtime_manifest.py",
        "scripts/runtime_ready.py",
    ):
        assert path in block
    assert "comfy-complete-runtime-materializer" in block
    assert "runtime-materializer-${HASH:0:16}" in block
    assert "--build-arg MATERIALIZER_DIGEST" in block
    assert 'docker push "$IMAGE:$CONTENT_TAG"' in block
    assert 'docker push "$IMAGE:${{ github.sha }}"' in block
    assert '-t "$IMAGE:latest"' not in block
    assert "OBJECT_STORE_" not in block
    assert "RUNPOD_" not in block

    for package in ("ca-certificates", "python3", "zstd"):
        assert package in dockerfile
    assert 'ENTRYPOINT ["/usr/bin/python3", "/runtime-tools/download_materialize_runtime.py"]' in dockerfile


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
    assert 'gate.get("static_policy")' in block
    assert 'static_policy.get("status") != "pass"' in block
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


def test_runtime_object_publisher_is_explicit_dispatch_only_and_secret_gated():
    workflow = DOCKER_BUILD.read_text()
    parsed = yaml.safe_load(workflow)
    trigger = parsed.get("on", parsed.get(True, {}))
    inputs = trigger["workflow_dispatch"]["inputs"]

    assert inputs["publish_runtime"] == {
        "description": "Publish the verified runtime archive to the configured object store",
        "required": False,
        "type": "boolean",
        "default": False,
    }
    assert inputs["channel"]["type"] == "string"
    assert inputs["channel"]["default"] == "staging"

    block = workflow.split("  publish-runtime-slim:\n", 1)[1]
    gate = (
        "if: ${{ github.event_name == 'workflow_dispatch' && "
        "github.ref == 'refs/heads/main' && inputs.publish_runtime == true }}"
    )
    publisher = block.split("      - name: Publish verified runtime archive to object store\n", 1)[1]
    publisher = publisher.split("      - name: Verify runtime publication result\n", 1)[0]
    assert publisher.count(gate) == 1
    assert "    environment: runtime-publication\n" in block
    assert "Validate runtime publication channel" in block
    assert "RUNTIME_CHANNEL" in publisher
    assert '"$RUNTIME_CHANNEL" == "."' in block
    assert '"$RUNTIME_CHANNEL" == ".."' in block
    for secret in (
        "OBJECT_STORE_ENDPOINT",
        "OBJECT_STORE_BUCKET",
        "OBJECT_STORE_ACCESS_KEY_ID",
        "OBJECT_STORE_SECRET_ACCESS_KEY",
    ):
        assert f"{secret}: ${{{{ secrets.{secret} }}}}" in publisher
    assert "--require-config" in publisher
    assert "S3_" not in publisher


def test_runtime_publication_preflight_is_manual_main_only_and_minimal():
    workflow = RUNTIME_PUBLICATION_PREFLIGHT.read_text()
    parsed = yaml.safe_load(workflow)
    trigger = parsed.get("on", parsed.get(True))
    assert trigger == {"workflow_dispatch": None}

    job = parsed["jobs"]["runtime-publication-preflight"]
    assert job["if"] == "${{ github.ref == 'refs/heads/main' }}"
    assert job["environment"] == "runtime-publication"
    assert job["permissions"] == {"contents": "read"}
    assert job["timeout-minutes"] <= 10
    assert "push:" not in workflow
    assert "pull_request:" not in workflow
    assert "workflow_run:" not in workflow
    assert "actions/checkout@v7" in workflow
    assert "persist-credentials: false" in workflow


def test_runtime_publication_preflight_requires_pinned_client_and_all_secrets():
    workflow = RUNTIME_PUBLICATION_PREFLIGHT.read_text()
    assert "scripts/runtime-publisher-requirements.txt" in workflow
    assert "--require-hashes" in workflow
    assert "--only-binary=:all:" in workflow
    assert "--no-deps" in workflow

    probe = workflow.split("      - name: Validate credentials with read-only bucket probes\n", 1)[1]
    for secret in (
        "OBJECT_STORE_ENDPOINT",
        "OBJECT_STORE_BUCKET",
        "OBJECT_STORE_ACCESS_KEY_ID",
        "OBJECT_STORE_SECRET_ACCESS_KEY",
        "OBJECT_STORE_REGION",
    ):
        assert f"{secret}: ${{{{ secrets.{secret} }}}}" in probe
    assert "set -euo pipefail" in workflow
    assert "parsed.scheme != \"https\"" in probe
    assert "endpoint_host" in probe
    assert "missing" in probe


def test_runtime_publication_preflight_performs_only_bounded_read_probes():
    workflow = RUNTIME_PUBLICATION_PREFLIGHT.read_text()
    assert "client.head_bucket(Bucket=bucket)" in workflow
    assert "client.list_objects_v2(Bucket=bucket, MaxKeys=1)" in workflow
    assert "connect_timeout=5" in workflow
    assert "read_timeout=10" in workflow
    assert '"max_attempts": 2' in workflow
    assert "logging.disable(logging.CRITICAL)" in workflow
    for forbidden in (
        "put_object",
        "delete_object",
        "create_multipart_upload",
        "upload_part",
        "complete_multipart_upload",
        "abort_multipart_upload",
        "copy_object",
    ):
        assert forbidden not in workflow
    assert "print(result" not in workflow
    assert "print(response" not in workflow


def test_runtime_object_publisher_runs_after_all_verification_and_image_push():
    workflow = DOCKER_BUILD.read_text()
    block = workflow.split("  publish-runtime-slim:\n", 1)[1]
    runtime_dir_setup = block.index('sudo install -d -o 0 -g "$(id -g)" -m 0770 /mnt/runtime-export')
    slim = block.index("Build and inspect the slim launcher image")
    inventory = block.index("Generate and verify the slim launcher inventory")
    slim_smoke = block.index("Smoke-test slim launcher image contract")
    export = block.index("Export and fully verify the immutable runtime")
    ready = block.index("scripts/runtime_ready.py")
    slim_metadata = block.index('python3 - "$SIZE" "$RUNNER_TEMP/slim-image.json"')
    export_cleanup = block.index('rm -rf "$RUNTIME_DIR"/*')
    slim_metadata_copy = block.index('cp "$RUNNER_TEMP/slim-image.json" "$RUNTIME_DIR/slim-image.json"')
    release = block.index("Release base and exporter images before local volume materialization")
    materialize = block.index("Materialize runtime into a local simulated Network Volume")
    materialized_smoke = block.index("Smoke-test materialized runtime through slim launcher contracts")
    remove_volume = block.index("Remove local simulated runtime volume after smoke")
    push = block.index("Push the immutable slim launcher image")
    publish = block.index("Publish verified runtime archive to object store")
    upload = block.index("Upload runtime publication metadata")
    cleanup = block.index("Delete the runner-local runtime archive")
    assert (
        runtime_dir_setup
        < slim
        < slim_metadata
        < inventory
        < slim_smoke
        < export
        < export_cleanup
        < ready
        < slim_metadata_copy
        < release
        < materialize
        < materialized_smoke
        < remove_volume
        < push
        < publish
        < upload
        < cleanup
    )
    assert 'python3 - "$SIZE" "${{ steps.identity.outputs.runtime_dir }}/slim-image.json"' not in block
    smoke_block = block.split("      - name: Smoke-test slim launcher image contract\n", 1)[1]
    smoke_block = smoke_block.split("      - name: Export and fully verify the immutable runtime\n", 1)[0]
    for path in (
        "/runtime-launcher.py",
        "/start-pod.sh",
        "/launcher-lib/runtime_manifest.py",
        "/pod-gateway.py",
        "/pod-model-bootstrap.py",
        "/pod-asset-sync.py",
        "/usr/local/bin/comfy-manager-set-mode",
    ):
        assert path in smoke_block
    assert '["/usr/bin/dumb-init","--","/runtime-launcher.py"]' in smoke_block
    assert "--network none" in smoke_block
    assert "--read-only" in smoke_block
    assert "--pids-limit 128" in smoke_block
    assert "--tmpfs /tmp:rw,nosuid,nodev,noexec,size=16m" in smoke_block
    assert "--entrypoint /bin/sh" in smoke_block
    assert "docker run --rm \\\n            -i \\\n" in smoke_block
    assert "bash -n /start-pod.sh" in smoke_block
    assert "PYTHONDONTWRITEBYTECODE=1" in smoke_block
    assert 'compile(source.read(), path, "exec")' in smoke_block
    assert "spec_from_file_location" in smoke_block
    assert "import runtime_manifest" in smoke_block
    assert "for command in bash python3 dumb-init env" in smoke_block
    assert "PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/launcher-lib python3 - <<'PY'" in smoke_block
    assert "python3 -c" not in smoke_block
    assert "--gpus" not in smoke_block
    assert "RUNPOD" not in smoke_block
    assert "runtime-publisher.json" in block
    assert "scripts/runtime-publisher-requirements.txt" in block
    assert "--require-hashes" in block
    assert "--only-binary=:all:" in block
    metadata = block.split("actions/upload-artifact@v4", 1)[1].split("Delete the runner-local runtime archive", 1)[0]
    assert ".tar.zst" not in metadata


def test_runtime_materializer_smoke_precedes_publisher_and_keeps_archive_local():
    workflow = DOCKER_BUILD.read_text()
    block = workflow.split("  publish-runtime-slim:\n", 1)[1]

    release = block.index("Release base and exporter images before local volume materialization")
    materialize = block.index("Materialize runtime into a local simulated Network Volume")
    smoke = block.index("Smoke-test materialized runtime through slim launcher contracts")
    remove_volume = block.index("Remove local simulated runtime volume after smoke")
    push = block.index("Push the immutable slim launcher image")
    publisher = block.index("Publish verified runtime archive to object store")
    cleanup = block.index("Delete the runner-local runtime archive")
    assert release < materialize < smoke < remove_volume < push < publisher < cleanup

    materializer_block = block.split(
        "      - name: Materialize runtime into a local simulated Network Volume\n", 1
    )[1].split(
        "      - name: Smoke-test materialized runtime through slim launcher contracts\n", 1
    )[0]
    assert "scripts/materialize_runtime.py" in materializer_block
    assert "--archive \"$ARCHIVE\"" in materializer_block
    assert "--manifest \"$RUNTIME_DIR/manifest.json\"" in materializer_block
    assert "--volume-root \"$RUNTIME_VOLUME\"" in materializer_block
    assert "runtime-materializer.json" in materializer_block
    assert "command -v zstd" in materializer_block
    assert "apt-get install -y --no-install-recommends zstd" in materializer_block
    assert 'sudo chown 0:0 "$RUNTIME_VOLUME" "$SMOKE_AUDIT"' in materializer_block
    assert "sudo python3 scripts/materialize_runtime.py" in materializer_block
    assert 'result.get("status") != "materialized"' in materializer_block
    assert 'result.get("current_updated") is not True' in materializer_block
    assert "rm -rf \"$RUNTIME_DIR\"" not in materializer_block
    assert "OBJECT_STORE_" not in materializer_block
    assert "secrets." not in materializer_block

    smoke_block = block.split(
        "      - name: Smoke-test materialized runtime through slim launcher contracts\n", 1
    )[1].split(
        "      - name: Remove local simulated runtime volume after smoke\n", 1
    )[0]
    for option in (
        "--network none",
        "--read-only",
        "--cap-drop ALL",
        "--security-opt no-new-privileges",
        "--tmpfs /tmp:rw,nosuid,nodev,noexec,size=256m,mode=1777",
        "--tmpfs /opt:rw,nosuid,nodev,size=16m,mode=0755",
        "--tmpfs /app:rw,nosuid,nodev,noexec,size=32m,mode=0755",
        '--mount type=bind,source="$RUNTIME_VOLUME",destination=/runpod-volume',
        "runtime_launcher",
        "load_verified_manifest",
        "verify_runtime_tree",
        "install_compatibility_link",
        'module.install_compatibility_link(Path("/app/comfyui"), runtime_root / "app/comfyui")',
        "runtime_critical_probe.py",
        "--profile cpu",
        "aiohttp",
        "py_compile.compile",
        "runtime bytecode was not written to the shared generation",
        "/opt/conda/bin/python",
    ):
        assert option in smoke_block
    assert "PYTHONDONTWRITEBYTECODE" not in smoke_block
    assert "--gpus" not in smoke_block
    assert "start.sh" not in smoke_block
    assert "OBJECT_STORE_" not in smoke_block
    assert "secrets." not in smoke_block
    assert "docker run --rm \\\n            -i \\\n" in smoke_block

    cleanup_block = block.split(
        "      - name: Remove local simulated runtime volume after smoke\n", 1
    )[1].split(
        "      - name: Push the immutable slim launcher image\n", 1
    )[0]
    assert "if: ${{ always() }}" in cleanup_block
    assert 'sudo rm -rf "$RUNTIME_VOLUME"' in cleanup_block
    assert 'sudo rm -rf "$SMOKE_AUDIT"' in cleanup_block

    upload = block.split("actions/upload-artifact@v4", 1)[1].split(
        "Delete the runner-local runtime archive", 1
    )[0]
    for metadata in (
        "runtime-materializer.json",
        "runtime-launcher-smoke.json",
        "runtime-critical-probe.json",
    ):
        assert metadata in upload
    assert ".tar.zst" not in upload


def test_runtime_materializer_documentation_is_offline_only_and_mentions_smoke_boundary():
    documentation = (REPO_ROOT / "docs" / "runtime-volume-materializer.md").read_text()
    assert "offline local `archive -> mounted volume`" in documentation
    assert "not an R2 downloader" in documentation
    assert "scripts/materialize_runtime.py" in documentation
    assert "READY.json" in documentation
    assert "current" in documentation
    assert "zstd" in documentation
    assert "read-write" in documentation


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
    assert "runtime-portability-gate.json" in block
    assert "--gate-policy" in block
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
    launcher = yaml.safe_load(inventory.read_text())
    assert launcher["library_paths"] == []
    assert launcher["libraries"] == sorted(set(launcher["libraries"]))
    assert {
        "ld-linux-x86-64.so.2",
        "libc.so.6",
        "libcuda.so.1",
        "libavcodec.so.60",
        "libavdevice.so.60",
        "libavfilter.so.9",
        "libavformat.so.60",
        "libavutil.so.58",
        "libcom_err.so.2",
        "libgpg-error.so.0",
        "libmvec.so.1",
        "libp11-kit.so.0",
        "libsox.so",
        "libXt.so.6",
    } <= set(launcher["libraries"])
    assert {"/bin/bash", "/bin/sh", "/usr/bin/dumb-init", "/usr/bin/env"} <= set(
        launcher["executable_paths"]
    )

    workflow = DOCKER_BUILD.read_text()
    inventory_block = workflow.split("- name: Generate and verify the slim launcher inventory", 1)[1].split(
        "- name: Smoke-test slim launcher image contract", 1
    )[0]
    assert '--user "$(id -u):$(id -g)"' in inventory_block
    assert "--injected-library libcuda.so.1" in inventory_block
    assert 'diff -u ci/runtime-launcher-inventory.json "$GENERATED"' in inventory_block

    publish_job = workflow.split("  publish-runtime-slim:\n", 1)[1]
    assert "    environment: runtime-publication\n" in publish_job
    assert publish_job.count("github.ref == 'refs/heads/main'") >= 2

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
        "/app/comfyui/.ce/envs/geometrypack-nodes/.pixi/envs/default/lib/icu/Makefile.inc",
        "/app/comfyui/.ce/envs/geometrypack-nodes/.pixi/envs/default/lib/icu/pkgdata.inc",
        "/app/comfyui/.ce/envs/sam3/.pixi/envs/default/lib/icu/Makefile.inc",
        "/app/comfyui/.ce/envs/sam3/.pixi/envs/default/lib/icu/pkgdata.inc",
    ]


def test_runtime_publisher_requirements_are_complete_hash_locked_wheels():
    requirements = [
        line.strip()
        for line in RUNTIME_PUBLISHER_REQUIREMENTS.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    expected = {
        "boto3",
        "botocore",
        "jmespath",
        "python-dateutil",
        "s3transfer",
        "six",
        "urllib3",
    }
    assert {line.split("==", 1)[0].lower() for line in requirements} == expected
    assert len(requirements) == len(expected)
    for line in requirements:
        assert "==" in line
        assert "--hash=sha256:" in line


def test_runtime_publisher_secrets_are_isolated_from_install_and_validation_steps():
    workflow = DOCKER_BUILD.read_text()
    prepare = workflow.split("- name: Prepare isolated runtime publisher environment\n", 1)[1].split(
        "- name: Publish verified runtime archive to object store\n", 1
    )[0]
    publish = workflow.split("- name: Publish verified runtime archive to object store\n", 1)[1].split(
        "- name: Verify runtime publication result\n", 1
    )[0]
    verify = workflow.split("- name: Verify runtime publication result\n", 1)[1].split(
        "- name: Upload runtime publication metadata\n", 1
    )[0]

    assert "OBJECT_STORE_" not in prepare
    assert "scripts/runtime-publisher-requirements.txt" in prepare
    assert "--require-hashes" in prepare
    assert "--only-binary=:all:" in prepare
    assert "--no-deps" in prepare
    assert "pip install" in prepare
    assert "OBJECT_STORE_" in publish
    assert publish.count("scripts/publish_runtime.py") == 1
    assert "pip install" not in publish
    assert "python3 -" not in publish
    assert "find " not in publish
    assert "OBJECT_STORE_" not in verify
    assert "scripts/publish_runtime.py" not in verify
    assert "steps.runtime-publisher-prepare.outputs.result" in verify


def test_runtime_publication_preflight_uses_the_same_secret_free_dependency_install():
    workflow = RUNTIME_PUBLICATION_PREFLIGHT.read_text()
    install = workflow.split("- name: Install hash-locked read-only S3 client\n", 1)[1].split(
        "- name: Validate credentials with read-only bucket probes\n", 1
    )[0]
    probe = workflow.split("- name: Validate credentials with read-only bucket probes\n", 1)[1]

    assert "actions/checkout@v7" in workflow
    assert "persist-credentials: false" in workflow
    assert "scripts/runtime-publisher-requirements.txt" in install
    assert "--require-hashes" in install
    assert "--only-binary=:all:" in install
    assert "--no-deps" in install
    assert "OBJECT_STORE_" not in install
    assert "OBJECT_STORE_ENDPOINT: ${{ secrets.OBJECT_STORE_ENDPOINT }}" in probe
    assert "pip install" not in probe
