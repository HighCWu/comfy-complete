# Comfy Complete

The reproducible runtime that [Comfy Cloud](https://comfy.org) deploys —
ComfyUI core, a curated set of custom node packs, and exact pinned Python
dependencies, in one repo and one Docker image.

This repo is public **for transparency**. It is the authoritative source for
what Comfy Cloud is running at any given commit. Cloud pulls the cc-base
image built from this repo (or builds it directly from this repo's contents).

> **Phase 1 status.** This repo's contents are managed by the Comfy Cloud
> team. We are not accepting external node submissions or dependency PRs at
> this stage — please file issues or reach us via the Comfy Cloud Discord if
> you have requests.

## What's inside

- **Pinned ComfyUI**: `version_lock.yaml` pins core + frontend + workflow templates.
- **Pinned Python deps**: `requirements.txt` — every package at an exact `==` version.
- **Curated custom node packs**: `supported_nodes.yaml` — each pack pinned to a specific version, with permission labels declared.
- **Reproducible Dockerfile**: `docker/Dockerfile.cloudbuild` produces the
  cc-base image that cloud's inference workers run.

## Quick Start

### Docker Compose (Recommended)

Edit the model volume path in `compose.yaml`, then:

```bash
docker compose up -d   # start
docker compose down    # stop
```

### Build the cc-base image yourself

```bash
docker build -f docker/Dockerfile.cloudbuild -t comfy-complete-base:local .
```

The build clones ComfyUI at the pinned ref, installs the pinned Python deps,
and installs every custom node pack in `supported_nodes.yaml`. Layer order is
tuned for cache reuse — re-running after only `supported_nodes.yaml` changes
should reuse most layers.

### Manual install (no Docker)

```bash
# Clone ComfyUI at the pinned ref
COMFY_REF=$(grep -A1 'comfyui:' version_lock.yaml | grep ref | cut -d'"' -f2)
git clone https://github.com/comfyanonymous/ComfyUI.git
(cd ComfyUI && git checkout "$COMFY_REF")

# Install pinned deps
pip install -r requirements.txt

# Install custom nodes
pip install comfy-cli pyyaml
python scripts/install_custom_nodes.py --comfy-path ./ComfyUI
```

## Repository Structure

```
comfy-complete/
├── compose.yaml             # local docker-compose
├── requirements.txt         # pinned Python dependencies
├── supported_nodes.yaml     # curated custom node packs + labels
├── version_lock.yaml        # ComfyUI core + frontend + templates pins
├── scripts/
│   └── install_custom_nodes.py
├── docker/
│   ├── Dockerfile           # local docker build
│   ├── Dockerfile.cloudbuild  # cc-base production build
│   └── entrypoint.sh
└── tests/                   # pin validation + label assertions
```

## Configuration files

### version_lock.yaml

```yaml
pinned:
  comfyui:
    ref: "<sha-or-tag>"
  comfyui_frontend_package:
    ref: "<version>"
  comfyui_workflow_templates:
    ref: "<version>"
```

### supported_nodes.yaml

```yaml
node_packs:
  - name: <comfy-registry-id>
    version: "<version>"
    node_labels:
      <NodeClassName>:
        - <Label1>
        - <Label2>
```

## Permission labels

Every node pack declares the permissions its nodes need. Comfy Cloud's
deployment policy decides which labels to disable based on the runtime
environment.

| Label | Meaning |
|-------|---------|
| `ReadsArbitraryFile` | Node reads from user-provided file paths |
| `WritesToDisk` | Node writes files to filesystem |
| `CreatesLargeOutputs` | Node produces large outputs (video, audio, models) |
| `NetworkAccess` | Node makes network requests |
| `RequiresExternalAPI` | Node requires external API keys |
| `Stateful` | Node persists user-specific data between runs |
| `HasCustomEndpoints` | Node registers custom HTTP server routes |
| `PathParsing` | Node exposes filesystem path information |
| `DuplicateOfCoreNode` | Node duplicates functionality of a core ComfyUI node |
| `Incompatible` | Node is incompatible with the distribution environment |
| `RequiresWebcam` | Node requires webcam hardware access |
| `RequiresDisplay` | Node requires interactive display or browser UI |
| `RequiresClipboard` | Node requires system clipboard access |
| `RequiresGPU` | Node hardcodes CUDA/GPU usage and will crash without one |
| `BrokenNode` | Node is currently broken or non-functional |
| `ExecutesArbitraryCode` | Node executes user-provided code (eval, exec, pickle, etc.) |
| `RuntimeModelDownload` | Node downloads models from the internet at execution time |
| `RuntimePipInstall` | Node installs Python packages via pip at execution time |

## Docker environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `COMFY_LISTEN_HOST` | `0.0.0.0` | Host to listen on |
| `COMFY_PORT` | `8188` | Port to listen on |
| `COMFY_PREVIEW_METHOD` | (none) | Preview method (latent2rgb, taesd, etc.) |
| `COMFY_EXTRA_ARGS` | (none) | Additional ComfyUI arguments |
| `COMFY_EXTRA_LIBS` | (none) | Additional pip packages installed at startup (testing only) |

## Tests

```bash
pip install pytest pyyaml
pytest tests/ -v
```

Tests verify: `requirements.txt` resolves, every package is pinned to an
exact version, YAML configs are valid, no known conflicting packages.

## License

Apache 2.0 — see [LICENSE](./LICENSE).
