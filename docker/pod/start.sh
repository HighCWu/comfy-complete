#!/usr/bin/env bash
set -euo pipefail

: "${COMFY_POD_PORT:=8189}"
: "${COMFY_LOG_LEVEL:=INFO}"

# There are two explicit serving modes:
#   * with COMFY_POD_TOKEN, ComfyUI stays on loopback and the authenticated gateway
#     serves COMFY_POD_PORT (managed model/asset sync additionally needs all fields);
#   * with no control-plane credentials at all, ComfyUI serves COMFY_POD_PORT directly
#     on 0.0.0.0 for a generic, standalone Pod deployment.
# A half-configured control plane without a token is rejected so it cannot
# accidentally turn into an unauthenticated public service.
managed_mode=0
gateway_mode=0
instance_id="${COMFY_INSTANCE_ID:-default}"
if [ -n "${COMFY_POD_TOKEN:-}" ]; then
    gateway_mode=1
    : "${COMFY_INTERNAL_HOST:=127.0.0.1}"
    : "${COMFY_INTERNAL_PORT:=8188}"
    if [ -n "${COMFY_INSTANCE_ID:-}" ] && [[ ! "${COMFY_INSTANCE_ID}" =~ ^inst_[A-Za-z0-9]+$ ]]; then
        echo "comfy-pod: invalid COMFY_INSTANCE_ID" >&2
        exit 64
    fi
    if [ -n "${COMFY_CONTROL_PLANE_URL:-}" ] && [ -n "${COMFY_INSTANCE_ID:-}" ]; then
        managed_mode=1
    elif [ -n "${COMFY_CONTROL_PLANE_URL:-}" ] || [ -n "${COMFY_INSTANCE_ID:-}" ]; then
        echo "comfy-pod: COMFY_CONTROL_PLANE_URL and COMFY_INSTANCE_ID must be configured together" >&2
        exit 64
    else
        echo "comfy-pod: no control-plane lease fields; running gateway-only" >&2
    fi
else
    if [ -n "${COMFY_CONTROL_PLANE_URL:-}" ] || [ -n "${COMFY_INSTANCE_ID:-}" ]; then
        echo "comfy-pod: COMFY_CONTROL_PLANE_URL or COMFY_INSTANCE_ID requires COMFY_POD_TOKEN" >&2
        exit 64
    fi
    : "${COMFY_INTERNAL_HOST:=0.0.0.0}"
    : "${COMFY_INTERNAL_PORT:=${COMFY_POD_PORT}}"
    echo "comfy-pod: no control-plane credentials; running standalone on ${COMFY_INTERNAL_HOST}:${COMFY_INTERNAL_PORT}" >&2
fi
if [[ ! "${instance_id}" =~ ^inst_[A-Za-z0-9]+$ ]]; then
    instance_id="default"
fi

# Establish the disposable per-instance data boundary before importing torch
# for the GPU preflight. The curated runtime itself remains writable on the
# shared Network Volume; only user input/output/state is kept instance-local.
instance_root="/tmp/comfy-runtime/${instance_id}"
echo "comfy-pod: disposable instance runtime at ${instance_root}"
mkdir -p \
    "${instance_root}/input" \
    "${instance_root}/output" \
    "${instance_root}/temp" \
    "${instance_root}/user" \
    "${instance_root}/home" \
    "${instance_root}/cache/cuda" \
    "${instance_root}/cache/huggingface" \
    "${instance_root}/cache/matplotlib" \
    "${instance_root}/cache/numba" \
    "${instance_root}/cache/pip" \
    "${instance_root}/cache/torch" \
    "${instance_root}/cache/transparent-background" \
    "${instance_root}/cache/triton" \
    "${instance_root}/cache/uv" \
    "${instance_root}/xdg/cache" \
    "${instance_root}/xdg/config" \
    "${instance_root}/xdg/data"
export HOME="${instance_root}/home"
export TMPDIR="${instance_root}/temp"
export TMP="${instance_root}/temp"
export TEMP="${instance_root}/temp"
export XDG_CACHE_HOME="${instance_root}/xdg/cache"
export XDG_CONFIG_HOME="${instance_root}/xdg/config"
export XDG_DATA_HOME="${instance_root}/xdg/data"
export CUDA_CACHE_PATH="${instance_root}/cache/cuda"
export HF_HOME="${instance_root}/cache/huggingface"
export HF_HUB_CACHE="${instance_root}/cache/huggingface/hub"
export HUGGINGFACE_HUB_CACHE="${instance_root}/cache/huggingface/hub"
export TRANSFORMERS_CACHE="${instance_root}/cache/huggingface/transformers"
export MPLCONFIGDIR="${instance_root}/cache/matplotlib"
export NUMBA_CACHE_DIR="${instance_root}/cache/numba"
export PIP_CACHE_DIR="${instance_root}/cache/pip"
export TORCH_HOME="${instance_root}/cache/torch"
export TRANSPARENT_BACKGROUND_FILE_PATH="${instance_root}/cache/transparent-background"
export TRITON_CACHE_DIR="${instance_root}/cache/triton"
export UV_CACHE_DIR="${instance_root}/cache/uv"

echo "comfy-pod: checking GPU availability"
python3 - <<'PY'
import torch

torch.cuda.init()
name = torch.cuda.get_device_name(0)
capability = torch.cuda.get_device_capability(0)
_ = (torch.zeros(8, device="cuda") + 1).sum().item()
torch.cuda.synchronize()
print(
    "comfy-pod: GPU available — "
    f"{name} (sm_{capability[0]}{capability[1]}), "
    f"torch {torch.__version__}, cuda {torch.version.cuda}"
)
PY

comfy_args=(
    --disable-auto-launch
    --disable-metadata
    --listen "${COMFY_INTERNAL_HOST}"
    --port "${COMFY_INTERNAL_PORT}"
    --verbose "${COMFY_LOG_LEVEL}"
    --log-stdout
)

# User inputs, outputs, temporary files, and ComfyUI state stay on the
# disposable container disk and are mirrored externally where persistence is
# required. The mounted Network Volume is reserved for managed shared caches.
# Keep ComfyUI-Manager state on the disposable per-instance filesystem.  The
# ComfyUI-Manager state is instance-specific even though the curated runtime
# itself is trusted and writable on the shared Network Volume.
export COMFYUI_MANAGER_CONFIG="${instance_root}/user/default/ComfyUI-Manager/config.ini"
comfy-manager-set-mode offline || \
    echo "comfy-pod: could not set ComfyUI-Manager network_mode" >&2

model_paths_config="/tmp/comfy-extra-model-paths.json"
if [ "${managed_mode}" -eq 1 ]; then
    python -u /pod-model-bootstrap.py \
        --instance-root "${instance_root}" \
        --config "${model_paths_config}" \
        --shared-volume-root /runpod-volume \
        --model-object-root /tmp/comfy-model-objects
    python -u /pod-asset-sync.py restore --instance-root "${instance_root}"
fi
comfy_args+=(
    --input-directory "${instance_root}/input"
    --output-directory "${instance_root}/output"
    --temp-directory "${instance_root}/temp"
    --user-directory "${instance_root}/user"
)
if [ "${managed_mode}" -eq 1 ]; then
    comfy_args+=(--extra-model-paths-config "${model_paths_config}")
fi

if [ -n "${COMFY_EXTRA_ARGS:-}" ]; then
    # Operator-controlled image configuration, never user input. Word splitting
    # is intentional so normal ComfyUI CLI flags can be supplied in one env var.
    # shellcheck disable=SC2206
    extra_args=( ${COMFY_EXTRA_ARGS} )
    comfy_args+=("${extra_args[@]}")
fi

python -u /comfyui/main.py "${comfy_args[@]}" &
comfy_pid=$!
gateway_pid=""
asset_sync_pid=""

cleanup() {
    if [ -n "${gateway_pid}" ]; then
        kill "${gateway_pid}" 2>/dev/null || true
    fi
    if [ -n "${asset_sync_pid}" ]; then
        kill "${asset_sync_pid}" 2>/dev/null || true
    fi
    kill "${comfy_pid}" 2>/dev/null || true
    wait "${gateway_pid}" 2>/dev/null || true
    wait "${asset_sync_pid}" 2>/dev/null || true
    wait "${comfy_pid}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "comfy-pod: waiting for ComfyUI on ${COMFY_INTERNAL_HOST}:${COMFY_INTERNAL_PORT}"
ready=0
for _ in $(seq 1 1200); do
    if ! kill -0 "${comfy_pid}" 2>/dev/null; then
        echo "comfy-pod: ComfyUI exited during startup" >&2
        wait "${comfy_pid}"
        exit 1
    fi
    if python3 - "${COMFY_INTERNAL_HOST}" "${COMFY_INTERNAL_PORT}" <<'PY'
import socket
import sys

with socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=1):
    pass
PY
    then
        ready=1
        break
    fi
    sleep 0.25
done

if [ "${ready}" -ne 1 ]; then
    echo "comfy-pod: ComfyUI did not become ready within 300 seconds" >&2
    exit 1
fi

if [ "${gateway_mode}" -eq 1 ]; then
    echo "comfy-pod: starting authenticated gateway on 0.0.0.0:${COMFY_POD_PORT}"
    python -u /pod-gateway.py &
    gateway_pid=$!
else
    echo "comfy-pod: COMFY_POD_TOKEN is not set; authenticated gateway is disabled" >&2
fi
if [ "${managed_mode}" -eq 1 ]; then
    python -u /pod-asset-sync.py watch --instance-root "${instance_root}" &
    asset_sync_pid=$!
fi

# The container is healthy only while both processes remain alive. Exiting when
# either child dies lets RunPod surface the failure instead of leaving a paid,
# unreachable Pod running indefinitely.
supervised_pids=("${comfy_pid}")
[ -n "${gateway_pid}" ] && supervised_pids+=("${gateway_pid}")
[ -n "${asset_sync_pid}" ] && supervised_pids+=("${asset_sync_pid}")
wait -n "${supervised_pids[@]}"
echo "comfy-pod: a supervised process exited; stopping Pod container" >&2
exit 1
