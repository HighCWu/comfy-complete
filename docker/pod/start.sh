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
worker_mode=0
instance_id="${COMFY_INSTANCE_ID:-default}"
worker_capability_secret="${COMFY_POD_WORKER_CAPABILITY_SECRET:-${COMFY_WORKER_CAPABILITY_SECRET:-${COMFY_POD_CAPABILITY_SECRET:-}}}"
worker_generation=0
worker_generation_file="${COMFY_POD_SUPERVISOR_GENERATION_FILE:-/tmp/comfy-pod-supervisor-generation}"
if [ -n "${worker_capability_secret}" ]; then
    worker_mode=1
    # Keep the canonical name in the child environment even when an early
    # compatibility alias was used.  The gateway never uses this as the user
    # facing Pod token.
    export COMFY_POD_WORKER_CAPABILITY_SECRET="${worker_capability_secret}"
fi
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
elif [ "${worker_mode}" -eq 1 ]; then
    # A worker-only capability is sufficient to expose the private lifecycle
    # protocol.  Keep ComfyUI on loopback because no browser proxy token was
    # configured for this intentionally internal-only mode.
    gateway_mode=1
    : "${COMFY_INTERNAL_HOST:=127.0.0.1}"
    : "${COMFY_INTERNAL_PORT:=8188}"
    echo "comfy-pod: worker capability interface enabled; browser gateway token is disabled" >&2
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
export COMFY_POD_INSTANCE_ROOT="${instance_root}"
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

# The optional worker protocol can request only this fixed, supervisor-owned
# signal.  It cannot pass a command or choose a PID.  Without the dedicated
# capability secret the ordinary gateway path remains unchanged.
if [ "${worker_mode}" -eq 1 ]; then
    export COMFY_POD_SUPERVISOR_PID="$$"
    export COMFY_POD_SUPERVISOR_GENERATION_FILE="${worker_generation_file}"
fi

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

start_comfy() {
    python -u /comfyui/main.py "${comfy_args[@]}" &
    comfy_pid=$!
}

wait_for_comfy() {
    echo "comfy-pod: waiting for ComfyUI on ${COMFY_INTERNAL_HOST}:${COMFY_INTERNAL_PORT}"
    ready=0
    for _ in $(seq 1 1200); do
        if ! kill -0 "${comfy_pid}" 2>/dev/null; then
            echo "comfy-pod: ComfyUI exited during startup" >&2
            wait "${comfy_pid}" || true
            return 1
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
        return 1
    fi
    return 0
}

publish_worker_generation() {
    if [ "${worker_mode}" -eq 1 ]; then
        worker_generation=$((worker_generation + 1))
        printf '%s\n' "${worker_generation}" > "${worker_generation_file}.next"
        mv -f "${worker_generation_file}.next" "${worker_generation_file}"
    fi
}

start_comfy
gateway_pid=""
asset_sync_pid=""
restart_requested=0

request_comfy_restart() {
    restart_requested=1
}

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
if [ "${worker_mode}" -eq 1 ]; then
    trap request_comfy_restart USR1
fi

if ! wait_for_comfy; then
    exit 1
fi
publish_worker_generation

if [ "${gateway_mode}" -eq 1 ]; then
    if [ -n "${COMFY_POD_TOKEN:-}" ]; then
        echo "comfy-pod: starting authenticated gateway on 0.0.0.0:${COMFY_POD_PORT}"
    else
        echo "comfy-pod: starting worker-capability gateway on 0.0.0.0:${COMFY_POD_PORT}"
    fi
    python -u /pod-gateway.py &
    gateway_pid=$!
else
    echo "comfy-pod: COMFY_POD_TOKEN is not set; authenticated gateway is disabled" >&2
fi
if [ "${managed_mode}" -eq 1 ]; then
    python -u /pod-asset-sync.py watch --instance-root "${instance_root}" &
    asset_sync_pid=$!
fi

# The container is healthy only while the gateway, asset sync, and ComfyUI
# children remain alive.  A reset failure may interrupt this wait with USR1;
# in that one controlled case, restart the fixed ComfyUI command, publish a
# new generation only after it is reachable, and then continue.  The gateway
# waits for that generation acknowledgement before probing a fresh baseline;
# no request can provide shell text or select another process.
while true; do
    supervised_pids=("${comfy_pid}")
    [ -n "${gateway_pid}" ] && supervised_pids+=("${gateway_pid}")
    [ -n "${asset_sync_pid}" ] && supervised_pids+=("${asset_sync_pid}")
    set +e
    wait -n "${supervised_pids[@]}"
    wait_status=$?
    set -e

    if [ "${restart_requested}" -eq 1 ] && [ "${worker_mode}" -eq 1 ]; then
        restart_requested=0
        echo "comfy-pod: controlled worker reset requested; restarting ComfyUI" >&2
        kill "${comfy_pid}" 2>/dev/null || true
        wait "${comfy_pid}" 2>/dev/null || true
        start_comfy
        if ! wait_for_comfy; then
            echo "comfy-pod: ComfyUI restart did not become ready" >&2
            exit 1
        fi
        publish_worker_generation
        continue
    fi

    echo "comfy-pod: a supervised process exited (status ${wait_status}); stopping Pod container" >&2
    exit 1
done
