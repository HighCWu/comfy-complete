# syntax=docker/dockerfile:1
# =====================================================
# Comfy Complete - ComfyUI Container
# A batteries-included ComfyUI distribution
#
# Layer order optimized for caching (least to most frequently changing):
# 0. SAM2 builder stage (rarely changes, expensive to build)
# 1. Base image + system deps
# 2. Base tools (uv, pyyaml, comfy-cli, git config)
# 3. ComfyUI clone
# 4. Python requirements (includes pre-built SAM2 wheel)
# 5. Custom nodes
# 6. Post-install config (build-config.yaml options like remove_manager)
# 7. Entrypoint + user setup
#
# Dev mode: Use --build-arg UV_CACHE=1 to enable uv package caching
# between builds (faster iteration, uses BuildKit cache mounts)
# =====================================================

# =============================================================================
# Stage 0: SAM2 Builder (cached separately - only rebuilds if commit changes)
# =============================================================================
# SAM2 requires compilation with CUDA, which is slow. Building it in a separate
# stage allows the wheel to be cached and reused across builds.
FROM pytorch/pytorch:2.8.0-cuda12.8-cudnn9-devel AS sam2-builder

ENV PYTHONDONTWRITEBYTECODE=1

# Build SAM2 wheel (pinned to specific commit for reproducibility)
ARG SAM2_COMMIT=2b90b9f5ceec907a1c18123530e92e794ad901a4
RUN pip wheel --no-build-isolation --no-deps \
    "SAM-2 @ https://github.com/facebookresearch/sam2/archive/${SAM2_COMMIT}.tar.gz" \
    -w /wheels

# =============================================================================
# Stage 1: Main image
# =============================================================================
FROM pytorch/pytorch:2.8.0-cuda12.8-cudnn9-devel

WORKDIR /app

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHON_PATH=/opt/conda/bin/python
ENV COMFY_PATH=/app/comfyui
ENV COMFY_PORT=8188
ENV UV_PYTHON=${PYTHON_PATH}

# UV settings
ENV UV_HTTP_TIMEOUT=300
ENV UV_HTTP_RETRIES=5
ENV UV_LINK_MODE=copy

# =============================================================================
# Layer 1: System dependencies (rarely changes)
# =============================================================================
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    wget \
    curl \
    ca-certificates \
    libssl-dev \
    bash \
    libgl1-mesa-glx \
    libglib2.0-0 \
    jq \
    dumb-init \
    libgomp1 \
    espeak-ng \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Create non-root user early (so we can create files owned by this user)
# comfy-cli needs a home directory for config storage
RUN groupadd --gid 1000 comfy \
    && useradd --uid 1000 --gid 1000 --create-home --shell /usr/sbin/nologin comfy

# =============================================================================
# Layer 2: Base tools (rarely changes)
# =============================================================================
# Install uv for faster dependency installation
COPY --from=ghcr.io/astral-sh/uv:0.9.26 /uv /uvx /usr/bin/

# Install base Python tools
# Cache mount persists between builds when using --build-arg UV_CACHE=1
# - yq: YAML parser (wrapper around jq) for reading version_lock.yaml
# - comfy-cli: ComfyUI management tool
RUN --mount=type=cache,target=/root/.cache/uv,id=uv-cache \
    uv pip install -v yq comfy-cli

# Debug: Check installed comfy-cli version (--no-deps requires >= 1.5.1)
RUN comfy --version

# Install pre-built SAM2 wheel from builder stage (fast - no compilation needed)
COPY --from=sam2-builder /wheels/*.whl /tmp/wheels/
RUN --mount=type=cache,target=/root/.cache/uv,id=uv-cache \
    uv pip install -v --no-deps /tmp/wheels/*.whl && \
    rm -rf /tmp/wheels

# =============================================================================
# Layer 3: ComfyUI clone (changes occasionally)
# =============================================================================
# Copy version lock file (readable by all users)
COPY --chmod=644 version_lock.yaml /app/version_lock.yaml

# Create base directory with correct ownership (files created as comfy user)
RUN mkdir -p ${COMFY_PATH} && chown comfy:comfy ${COMFY_PATH}

# Set XDG directories for custom nodes that store user-specific data
ENV XDG_DATA_HOME=/app/comfyui/models/_xdgdata \
    XDG_CACHE_HOME=/app/comfyui/models/_xdgcache \
    XDG_CONFIG_HOME=/app/comfyui/models/_xdgconfig \
    TRANSPARENT_BACKGROUND_FILE_PATH=/app/comfyui/models

# Switch to comfy user for git clone (files created with correct ownership)
USER comfy

# Clone ComfyUI and checkout specific version (using shallow clone for faster builds)
# Works with both commit hashes and tags (e.g., "v0.3.75")
RUN COMFYUI_REF=$(yq -r '.pinned.comfyui.ref' /app/version_lock.yaml) && \
    echo "Cloning ComfyUI and checking out ref: ${COMFYUI_REF}" && \
    git clone --depth 1 https://github.com/Comfy-Org/ComfyUI.git ${COMFY_PATH} && \
    cd ${COMFY_PATH} && \
    git fetch --depth 1 origin "${COMFYUI_REF}" && \
    git checkout FETCH_HEAD

# Create ComfyUI directories (as comfy user)
RUN cd ${COMFY_PATH} && \
    mkdir -p models/checkpoints \
    models/clip \
    models/controlnet \
    models/diffusers \
    models/embeddings \
    models/loras \
    models/upscale_models \
    models/vae \
    models/sams \
    models/insightface \
    models/detection \
    output \
    temp \
    models/_xdgdata \
    models/_xdgcache \
    models/_xdgconfig \
    custom_nodes

# Switch back to root for pip installs
USER root

# =============================================================================
# Layer 4: Python requirements (changes frequently)
# =============================================================================
COPY requirements.txt /app/requirements.txt

# Pin all dependencies to exact versions for deterministic builds
# -v flag shows granular progress during installation
RUN --mount=type=cache,target=/root/.cache/uv,id=uv-cache \
    uv pip install -v --no-deps -r /app/requirements.txt

# Install nvidia-vfx from NVIDIA's PyPI index (binary wheel includes Maxine VFX SDK libs)
# Installed separately to avoid exposing all packages to the extra index (dependency confusion risk)
RUN --mount=type=cache,target=/root/.cache/uv,id=uv-cache \
    uv pip install -v --no-deps --extra-index-url https://pypi.nvidia.com/ nvidia-vfx==0.1.0.1

# Install frontend and workflow templates from version_lock.yaml (single source of truth)
RUN --mount=type=cache,target=/root/.cache/uv,id=uv-cache \
    FRONTEND_REF=$(yq -r '.pinned.comfyui_frontend_package.ref' /app/version_lock.yaml) && \
    TEMPLATES_REF=$(yq -r '.pinned.comfyui_workflow_templates.ref' /app/version_lock.yaml) && \
    echo "Installing comfyui_frontend_package==${FRONTEND_REF}" && \
    echo "Installing comfyui_workflow_templates==${TEMPLATES_REF}" && \
    uv pip install -v --no-deps \
        comfyui_frontend_package==${FRONTEND_REF} \
        comfyui_workflow_templates==${TEMPLATES_REF}

# =============================================================================
# Layer 5: Custom nodes (changes most frequently)
# =============================================================================
COPY --chmod=644 supported_nodes.yaml /app/supported_nodes.yaml
COPY --chmod=755 scripts/install_custom_nodes.py /app/install_custom_nodes.py

# Fix ownership of XDG directories created by root's uv pip install in Layer 4.
# uv writes its cache to $XDG_CACHE_HOME/uv, which is /app/comfyui/models/_xdgcache/uv.
# Without this chown, the comfy user in Layer 5 gets "Permission denied" when uv
# tries to initialize the cache during custom node installation.
RUN chown -R comfy:comfy /app/comfyui/models/_xdgcache /app/comfyui/models/_xdgdata /app/comfyui/models/_xdgconfig

# Switch to comfy user for custom nodes installation (files created with correct ownership)
USER comfy

# Create the skip_download_model file to prevent model downloads during install
RUN touch ${COMFY_PATH}/custom_nodes/skip_download_model

# Install custom nodes with --no-deps for production builds
RUN cd ${COMFY_PATH} && ${PYTHON_PATH} /app/install_custom_nodes.py --no-deps

# =============================================================================
# Layer 5b: Pre-download models that nodes fetch at import time
# =============================================================================
# Some custom nodes download model files during __init__.py import, which
# blocks ComfyUI startup and can hang in air-gapped environments.
# Pre-bake these models into the image so startup is fast and reliable.

# comfyui_face_parsing: downloads 339MB model + config at import time
RUN mkdir -p ${COMFY_PATH}/models/face_parsing ${COMFY_PATH}/models/ultralytics/bbox && \
    wget -q -O ${COMFY_PATH}/models/face_parsing/model.safetensors \
        "https://huggingface.co/jonathandinu/face-parsing/resolve/main/model.safetensors" && \
    wget -q -O ${COMFY_PATH}/models/face_parsing/config.json \
        "https://huggingface.co/jonathandinu/face-parsing/resolve/main/config.json" && \
    wget -q -O ${COMFY_PATH}/models/face_parsing/preprocessor_config.json \
        "https://huggingface.co/jonathandinu/face-parsing/resolve/main/preprocessor_config.json" && \
    wget -q -O ${COMFY_PATH}/models/ultralytics/bbox/face_yolov8m.pt \
        "https://huggingface.co/Bingsu/adetailer/resolve/main/face_yolov8m.pt"

# onebuttonprompt: __init__.py expects dir named "OneButtonPrompt" but
# comfy-cli installs as lowercase "onebuttonprompt"
RUN if [ -d "${COMFY_PATH}/custom_nodes/onebuttonprompt" ] && \
       [ ! -d "${COMFY_PATH}/custom_nodes/OneButtonPrompt" ]; then \
        ln -s onebuttonprompt ${COMFY_PATH}/custom_nodes/OneButtonPrompt; \
    fi

# =============================================================================
# Layer 6: Post-install configuration (build config options)
# =============================================================================
# ARG declared here to avoid invalidating earlier cached layers
ARG REMOVE_MANAGER=0

# Optionally remove ComfyUI Manager (for locked-down cloud deployments)
# Use --build-arg REMOVE_MANAGER=1 to enable
RUN if [ "$REMOVE_MANAGER" = "1" ]; then \
        echo "Removing ComfyUI Manager..." && \
        rm -rf ${COMFY_PATH}/custom_nodes/ComfyUI-Manager; \
    fi

# Switch back to root for final setup
USER root

# Allow COMFY_EXTRA_LIBS to be installed at runtime
RUN chown -hR comfy:comfy /opt/conda/bin /opt/conda/lib/python3.11/site-packages /app/comfyui/models

# =============================================================================
# Layer 7: Entrypoint (rarely changes)
# =============================================================================
COPY docker/entrypoint.sh /app/entrypoint.sh
RUN chmod 755 /app/entrypoint.sh

# Expose ComfyUI port
EXPOSE ${COMFY_PORT}

# Set working directory to ComfyUI
WORKDIR ${COMFY_PATH}

# Run as non-root user
USER comfy

ENTRYPOINT ["/usr/bin/dumb-init", "--", "/app/entrypoint.sh"]
