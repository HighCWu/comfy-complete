#!/usr/bin/env bash
set -euo pipefail

if [ -z "${LAIMON_POD_TOKEN:-}" ]; then
    echo "laimon-pod: LAIMON_POD_TOKEN is required" >&2
    exit 1
fi
if [ -z "${LAIMON_CONTROL_PLANE_URL:-}" ]; then
    echo "laimon-pod: LAIMON_CONTROL_PLANE_URL is required" >&2
    exit 1
fi

: "${COMFY_INTERNAL_HOST:=127.0.0.1}"
: "${COMFY_INTERNAL_PORT:=8188}"
: "${LAIMON_POD_PORT:=8189}"
: "${COMFY_LOG_LEVEL:=INFO}"

if [ -z "${LAIMON_INSTANCE_ID:-}" ]; then
    echo "laimon-pod: LAIMON_INSTANCE_ID is required" >&2
    exit 1
fi
if [[ ! "${LAIMON_INSTANCE_ID}" =~ ^inst_[A-Za-z0-9]+$ ]]; then
    echo "laimon-pod: invalid LAIMON_INSTANCE_ID" >&2
    exit 1
fi

echo "laimon-pod: checking GPU availability"
python3 - <<'PY'
import torch

torch.cuda.init()
name = torch.cuda.get_device_name(0)
capability = torch.cuda.get_device_capability(0)
_ = (torch.zeros(8, device="cuda") + 1).sum().item()
torch.cuda.synchronize()
print(
    "laimon-pod: GPU available — "
    f"{name} (sm_{capability[0]}{capability[1]}), "
    f"torch {torch.__version__}, cuda {torch.version.cuda}"
)
PY

comfy-manager-set-mode offline || \
    echo "laimon-pod: could not set ComfyUI-Manager network_mode" >&2

comfy_args=(
    --disable-auto-launch
    --disable-metadata
    --listen "${COMFY_INTERNAL_HOST}"
    --port "${COMFY_INTERNAL_PORT}"
    --verbose "${COMFY_LOG_LEVEL}"
    --log-stdout
)

# User inputs, outputs, temporary files, and ComfyUI state are disposable
# runtime data. They stay on the container disk and are mirrored to R2 where
# persistence is required. A mounted network volume is reserved exclusively
# for platform-managed, content-addressed public models.
instance_root="/tmp/laimon-runtime/${LAIMON_INSTANCE_ID}"
echo "laimon-pod: disposable instance runtime at ${instance_root}"

mkdir -p \
    "${instance_root}/input" \
    "${instance_root}/output" \
    "${instance_root}/temp" \
    "${instance_root}/user"
model_paths_config="/tmp/laimon-extra-model-paths.json"
python -u /pod-model-bootstrap.py \
    --instance-root "${instance_root}" \
    --config "${model_paths_config}" \
    --comfy-model-root /app/comfyui/models \
    --shared-volume-root /runpod-volume
python -u /pod-asset-sync.py restore --instance-root "${instance_root}"
comfy_args+=(
    --input-directory "${instance_root}/input"
    --output-directory "${instance_root}/output"
    --temp-directory "${instance_root}/temp"
    --user-directory "${instance_root}/user"
    --extra-model-paths-config "${model_paths_config}"
)

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

echo "laimon-pod: waiting for ComfyUI on ${COMFY_INTERNAL_HOST}:${COMFY_INTERNAL_PORT}"
ready=0
for _ in $(seq 1 1200); do
    if ! kill -0 "${comfy_pid}" 2>/dev/null; then
        echo "laimon-pod: ComfyUI exited during startup" >&2
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
    echo "laimon-pod: ComfyUI did not become ready within 300 seconds" >&2
    exit 1
fi

echo "laimon-pod: starting authenticated gateway on 0.0.0.0:${LAIMON_POD_PORT}"
python -u /pod-gateway.py &
gateway_pid=$!
python -u /pod-asset-sync.py watch --instance-root "${instance_root}" &
asset_sync_pid=$!

# The container is healthy only while both processes remain alive. Exiting when
# either child dies lets RunPod surface the failure instead of leaving a paid,
# unreachable Pod running indefinitely.
wait -n "${comfy_pid}" "${gateway_pid}" "${asset_sync_pid}"
echo "laimon-pod: a supervised process exited; stopping Pod container" >&2
exit 1
