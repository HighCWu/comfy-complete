# SageAttention multi-arch wheel builder
#
# Builds a single wheel with CUDA kernels for ALL supported GPU architectures
# (sm_80, sm_86, sm_89, sm_90, sm_120). Run on CI without GPU — v2.2.0+
# setup.py reads TORCH_CUDA_ARCH_LIST env var natively.
#
# Output: scratch image containing only /wheels/*.whl
# Consumed by Dockerfile.cloudbuild via:
#   COPY --from=ghcr.io/highcwu/comfy-complete-sageattn:2.2.0 /wheels /tmp/
#
# Build:
#   docker build -f docker/sageattn.Dockerfile -t ghcr.io/highcwu/comfy-complete-sageattn:2.2.0 .
# Push:
#   docker push ghcr.io/highcwu/comfy-complete-sageattn:2.2.0
#
# Rebuild only when SAGEATTN_VERSION changes.

ARG BASE_IMAGE=pytorch/pytorch:2.8.0-cuda12.8-cudnn9-devel@sha256:a7103283ea7113e10ae5d014bd2342acebda0bc53164b2f7b1dd6eb7a766bdb6
FROM ${BASE_IMAGE} AS builder

ENV PYTHONDONTWRITEBYTECODE=1
ARG SAGEATTN_VERSION=2.2.0

# Target GPU architectures (Ampere/Ada/Hopper/Blackwell)
# v2.2.0+ setup.py reads TORCH_CUDA_ARCH_LIST — no patching needed.
ENV TORCH_CUDA_ARCH_LIST="8.0;8.6;8.9;9.0;12.0"

# Cap build parallelism for CI runners (2 vCPU / 7 GB RAM).
# v2.2.0 hardcodes nvcc --threads=8 and defaults to MAX_JOBS=32, which
# exhausts all memory + swap on GitHub runners → "runner lost contact".
#
# Three previous CI attempts all died from memory/CPU starvation:
#   - parallel=4, MAX_JOBS=32: killed after 6 min
#   - EXT_PARALLEL=1, MAX_JOBS=32: killed after 56 min
#   - EXT_PARALLEL=1, MAX_JOBS=4: killed after 60 min (--threads=8 still OOMs)
#
# The real memory driver is nvcc's --threads=8 (8 parallel .cu compilations
# per invocation). MAX_JOBS alone can't control it. Override via
# NVCC_APPEND_FLAGS="--threads=1" to force single-threaded nvcc.
#
# Final config: 2 parallel nvcc invocations × 1 thread each = ~2-4 GB peak.
# Fits in 7 GB RAM with zero swap pressure.
ENV EXT_PARALLEL=1
ENV MAX_JOBS=2
ENV NVCC_APPEND_FLAGS="--threads=1"

# Install git (not in pytorch:devel base) + ca-certificates for HTTPS
RUN apt-get update && \
    apt-get install -y --no-install-recommends git ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Clone SageAttention source at pinned tag
RUN git clone --depth 1 --branch v${SAGEATTN_VERSION} \
    https://github.com/thu-ml/SageAttention.git /sageattn

# Build wheel — no build isolation so it reuses torch/CUDA from the base image
# Compiles for sm_80 + sm_86 + sm_89 + sm_90 + sm_120.
# With MAX_JOBS=2 + --threads=1 on a 2-vCPU runner: ~20-40 min, zero swap.
RUN cd /sageattn && \
    pip wheel --no-build-isolation --no-deps . -w /wheels

# ─────────────────────────────────────────────────────────────────────
# Export stage: scratch image containing only the wheel(s)
# ─────────────────────────────────────────────────────────────────────
FROM scratch

COPY --from=builder /wheels /wheels
