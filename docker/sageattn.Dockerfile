# SageAttention multi-arch wheel builder
#
# Builds a single wheel with CUDA kernels for ALL supported GPU architectures
# (sm_80, sm_86, sm_89, sm_90, sm_120). Run on CI without GPU — setup.py is
# patched to skip auto-detection.
#
# Output: scratch image containing only /wheels/*.whl
# Consumed by Dockerfile.cloudbuild via:
#   COPY --from=ghcr.io/highcwu/comfy-complete-sageattn:latest /wheels /tmp/
#
# Build:
#   docker build -f docker/sageattn.Dockerfile -t ghcr.io/highcwu/comfy-complete-sageattn:latest .
# Push:
#   docker push ghcr.io/highcwu/comfy-complete-sageattn:latest
#
# Rebuild only when SAGEATTN_VERSION changes.

ARG BASE_IMAGE=pytorch/pytorch:2.8.0-cuda12.8-cudnn9-devel@sha256:a7103283ea7113e10ae5d014bd2342acebda0bc53164b2f7b1dd6eb7a766bdb6
FROM ${BASE_IMAGE} AS builder

ENV PYTHONDONTWRITEBYTECODE=1
ARG SAGEATTN_VERSION=2.2.0

# Install git (not in pytorch:devel base) + ca-certificates for HTTPS
RUN apt-get update && \
    apt-get install -y --no-install-recommends git ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Clone SageAttention source at pinned tag
RUN git clone --depth 1 --branch v${SAGEATTN_VERSION} \
    https://github.com/thu-ml/SageAttention.git /sageattn

# Patch setup.py: replace GPU auto-detection with hardcoded arch list
# (CI runners have no GPU → torch.cuda.device_count() returns 0 → RuntimeError)
COPY docker/patch_sageattn.py /tmp/patch_sageattn.py
RUN python /tmp/patch_sageattn.py /sageattn/setup.py

# Build wheel — no build isolation so it reuses torch/CUDA from the base image
# Compiles for sm_80 + sm_86 + sm_89 + sm_90 + sm_120 (takes ~5-10 min)
RUN cd /sageattn && \
    pip wheel --no-build-isolation --no-deps . -w /wheels

# ─────────────────────────────────────────────────────────────────────
# Export stage: scratch image containing only the wheel(s)
# ─────────────────────────────────────────────────────────────────────
FROM scratch

COPY --from=builder /wheels /wheels
