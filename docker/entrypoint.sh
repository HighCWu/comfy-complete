#!/bin/bash
set -e

# Comfy Complete - Container Entrypoint
# Starts ComfyUI with configurable options

echo "========== Comfy Complete =========="

if [ -n "${COMFY_EXTRA_LIBS}" ]; then
  echo "Installing ComfyUI extra libraries \"${COMFY_EXTRA_LIBS}\"..."
  echo "WARNING: This is a development/test feature only."
  echo "         NEVER use it in production!"
  echo "         Consider building a custom image for production use."
  uv pip install --no-deps ${COMFY_EXTRA_LIBS}
fi

echo "Starting ComfyUI..."

# Default values
LISTEN_HOST="${COMFY_LISTEN_HOST:-0.0.0.0}"
LISTEN_PORT="${COMFY_PORT:-8188}"

# Build command arguments
CMD_ARGS="--listen ${LISTEN_HOST} --port ${LISTEN_PORT}"

# Add preview method if specified
if [ -n "${COMFY_PREVIEW_METHOD}" ]; then
  CMD_ARGS="${CMD_ARGS} --preview-method ${COMFY_PREVIEW_METHOD}"
fi

# Add extra arguments if provided
if [ -n "${COMFY_EXTRA_ARGS}" ]; then
  CMD_ARGS="${CMD_ARGS} ${COMFY_EXTRA_ARGS}"
fi

echo "Command: python main.py ${CMD_ARGS}"
echo "===================================="

# Execute ComfyUI
exec python main.py ${CMD_ARGS}
