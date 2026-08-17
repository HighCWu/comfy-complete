"""Static contracts for the thin Pod supervisor's mutable state boundary."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
START = ROOT / "docker" / "pod" / "start.sh"
SLIM_DOCKERFILE = ROOT / "docker" / "Dockerfile.pod-slim"


def test_manager_config_is_redirected_to_disposable_instance_state() -> None:
    script = START.read_text(encoding="utf-8")
    instance_root = script.index('instance_root="')
    create_dirs = script.index('"${instance_root}/user"', instance_root)
    configure_manager = script.index('export COMFYUI_MANAGER_CONFIG=', create_dirs)
    set_mode = script.index("comfy-manager-set-mode offline", configure_manager)

    assert configure_manager < set_mode
    assert 'COMFYUI_MANAGER_CONFIG="${instance_root}/user/' in script


def test_pod_start_does_not_write_manager_state_before_instance_root_exists() -> None:
    script = START.read_text(encoding="utf-8")
    gpu_preflight = script.index('echo "comfy-pod: checking GPU availability"')
    set_mode = script.index("comfy-manager-set-mode offline")
    instance_root = script.index('instance_root="')
    create_dirs = script.index('"${instance_root}/user"', instance_root)

    assert instance_root < create_dirs < gpu_preflight < set_mode


def test_instance_specific_cache_roots_stay_on_disposable_state() -> None:
    script = START.read_text(encoding="utf-8")
    create_dirs = script.index('"${instance_root}/xdg/data"')

    for assignment in (
        'export HOME="${instance_root}/home"',
        'export TMPDIR="${instance_root}/temp"',
        'export XDG_CACHE_HOME="${instance_root}/xdg/cache"',
        'export XDG_CONFIG_HOME="${instance_root}/xdg/config"',
        'export XDG_DATA_HOME="${instance_root}/xdg/data"',
        'export CUDA_CACHE_PATH="${instance_root}/cache/cuda"',
        'export HF_HOME="${instance_root}/cache/huggingface"',
        'export TORCH_HOME="${instance_root}/cache/torch"',
        'export TRITON_CACHE_DIR="${instance_root}/cache/triton"',
        'export UV_CACHE_DIR="${instance_root}/cache/uv"',
    ):
        assert script.index(assignment) > create_dirs

    dockerfile = SLIM_DOCKERFILE.read_text(encoding="utf-8")
    assert "PYTHONPYCACHEPREFIX" not in script
    assert "PYTHONDONTWRITEBYTECODE" not in script
    assert "PYTHONDONTWRITEBYTECODE" not in dockerfile


def test_slim_launcher_installs_every_system_tool_used_before_runtime_linking() -> None:
    dockerfile = SLIM_DOCKERFILE.read_text(encoding="utf-8")
    install = dockerfile[dockerfile.index("apt-get install"):dockerfile.index("rm -rf /var/lib/apt/lists")]

    # These are launcher/supervisor dependencies, not assumptions about the
    # large runtime archive.  /bin/sh and /usr/bin/env are supplied by the
    # pinned Ubuntu base; the explicitly installed packages cover the rest.
    for package in ("bash", "coreutils", "dumb-init", "grep", "python3", "sed"):
        assert package in install
    assert "ENTRYPOINT [\"/usr/bin/dumb-init\", \"--\", \"/runtime-launcher.py\"]" in dockerfile


def test_managed_integrations_are_optional_without_an_unbound_instance_id() -> None:
    script = START.read_text(encoding="utf-8")

    assert 'instance_id="${COMFY_INSTANCE_ID:-default}"' in script
    assert 'if [ "${managed_mode}" -eq 1 ]; then' in script
    assert "--extra-model-paths-config" in script
    assert 'if [ -n "${COMFY_POD_TOKEN:-}" ]; then' in script


def test_pod_start_has_explicit_gateway_and_standalone_serving_modes() -> None:
    script = START.read_text(encoding="utf-8")

    assert ': "${COMFY_INTERNAL_HOST:=127.0.0.1}"' in script
    assert ': "${COMFY_INTERNAL_PORT:=8188}"' in script
    assert ': "${COMFY_INTERNAL_HOST:=0.0.0.0}"' in script
    assert ': "${COMFY_INTERNAL_PORT:=${COMFY_POD_PORT}}"' in script
    assert 'if [ "${gateway_mode}" -eq 1 ]; then' in script
    assert "requires COMFY_POD_TOKEN" in script
    assert "COMFY_CONTROL_PLANE_URL and COMFY_INSTANCE_ID must be configured together" in script
    assert "no control-plane lease fields; running gateway-only" in script
