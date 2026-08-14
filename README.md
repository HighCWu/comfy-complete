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

The Pod wrapper also supports optional managed integrations. `COMFY_CONTROL_PLANE_URL`,
`COMFY_INSTANCE_ID`, and `COMFY_POD_TOKEN` must be supplied together to enable model
bootstrap and input/output asset synchronization. `COMFY_POD_TOKEN` alone enables
the authenticated gateway; `COMFY_CONTROL_PLANE_URL` or `COMFY_INSTANCE_ID`
without that token is rejected. With no control-plane credentials, the wrapper
serves ordinary ComfyUI directly on `COMFY_POD_PORT`. `R2_ENDPOINT`, `R2_BUCKET`,
`R2_ACCESS_KEY_ID`, and `R2_SECRET_ACCESS_KEY` likewise form an all-or-nothing
optional output-upload integration; incomplete configuration falls back to the
RunPod-compatible base64 result path.

## Tests

```bash
pip install pytest pyyaml
pytest tests/ -v
```

Tests verify: `requirements.txt` resolves, every package is pinned to an
exact version, YAML configs are valid, no known conflicting packages.

## Flattened runtime export (local tool)

The production `docker/Dockerfile.cloudbuild` `base` image is the source of
the final runtime file tree. The export helper works on a materialized final
filesystem, never on OCI history layers:

```bash
python scripts/export_runtime.py \
  --source-root /path/to/final-rootfs \
  --output-dir /path/to/runtime-output \
  --runtime-version base-<version> \
  --source-image ghcr.io/example/comfy-complete-base \
  --source-image-digest sha256:<64-lowercase-hex> \
  --build-sha <40-lowercase-hex> \
  --launcher-digest sha256:<64-lowercase-hex> \
  --launcher-abi comfy-launcher/v1 \
  --entrypoint /app/comfyui/PLACEHOLDER_ENTRYPOINT
```

The default selection is `/opt/conda` and `/app/comfyui`, with mutable
instance paths excluded. Additional files under `/app` must be named with
repeated `--include-app`; broad roots such as `/etc` and `/usr/local/cuda`
are not accepted; replace `PLACEHOLDER_ENTRYPOINT` with the launcher path
from the runtime design before running. The command requires the `zstd` CLI
and writes a
content-addressed `sha256-<archive-sha256>.tar.zst`. The manifest is named by
the canonical final file-tree runtime digest plus the archive digest
(`sha256-<tree-sha256>-<archive-sha256>.json`), while archive digest/size are
recorded independently. Hard links are copied as ordinary files in v1, so
repeated content can increase bundle size.
The exporter itself remains local and provider-neutral. The public workflow
can pass its verified output to the separately gated object publisher, but it
is not wired to RunPod or Pod hydration. Use `--verify ARCHIVE MANIFEST` to
fail closed on a missing, corrupt, or inconsistent export.

## Runtime object publisher (explicit opt-in)

After the export and `READY.json` checks pass, the independent
`scripts/publish_runtime.py` helper can publish the archive to an
S3-compatible object store. It uses content-addressed immutable objects,
bounded sequential multipart upload, post-upload HEAD verification, and a
small channel pointer written last. Missing object-store credentials produce a
safe `skipped` result; partial credentials fail closed. It never deletes old
versions or copies the large archive. See
[`docs/runtime-object-publisher.md`](docs/runtime-object-publisher.md) for
configuration and the explicit workflow integration point.

## Runtime portability audit (local, read-only)

After materializing the final `base` rootfs, audit the same selection before
exporting it:

```bash
python scripts/runtime_portability_audit.py \
  --source-root /path/to/final-rootfs \
  --selection-policy /path/to/runtime-manifest.json \
  --launcher-inventory /path/to/launcher-inventory.json \
  --gate-policy ci/runtime-portability-gate.json \
  --output /path/to/runtime-portability.json
```

The audit reports deterministic blocker/warning/info findings for executable
absolute shebangs, ELF `PT_INTERP`, `DT_NEEDED`, `RPATH`, and `RUNPATH`, and
lists the system paths, libraries, and library directories the minimal
launcher image must provide. It also counts selected symlinks. The rootfs is
never modified; no image, Actions, R2, or RunPod operation is performed.
Missing `readelf` or a bounded inspection failure is reported as a limited
audit rather than silently treated as success; `limited` is a non-zero CLI
result and blocks publishing until the audit is complete. The raw report keeps
every finding. The optional `--gate-policy` is a separate, reviewed,
deny-by-default policy: an allowance contains the SHA-256 identity of the
complete deterministic finding, not a path glob. A new or modified finding,
duplicate allowance, or incomplete report remains a gate blocker. The
checked-in bootstrap policy intentionally allows nothing until a fresh report
from the exact immutable base image has been reviewed. The policy is also
bound to the audit implementation version and the `base` critical profile so
allowances cannot silently cross scanner/profile contracts. Static
`runtime_library_present_unresolved` findings are warnings when a selected
runtime candidate exists; the critical import probe is the dynamic proof for
the base startup closure, while genuinely missing libraries remain blockers.

### Public Docker Build audit

The `Docker Build` workflow has a separate `audit-base-runtime` job. It waits
for `build-base`, reuses that job's content-addressed `base-*` tag, pulls the
published `Dockerfile.cloudbuild` `base` image, and runs the audit inside that
exact image with `--source-root /`. The container is read-only, has no network,
and mounts only the audit script, policy, inventory, and small output directory;
the audit input is therefore the final image rootfs, not this checkout. This
avoids copying the multi-gigabyte runtime to the runner filesystem while
preserving the final-container boundary. The job uses the checked-in Ubuntu
launcher inventory (`ci/runtime-launcher-inventory.json`) plus the
deny-by-default static gate policy (`ci/runtime-portability-gate.json`).
Provider-injected CUDA driver names and launcher-provided shell/coreutils
paths are explicit inventory evidence; any dependency outside that contract
or outside an exact reviewed finding allowance fails the release gate.

Only small JSON metadata/policy files, the portability report, and its log are
uploaded as a short-retention workflow artifact. The materialized rootfs and
the archive from `export_runtime.py` are never uploaded. The audit job has only
`contents: read` and `packages: read` permissions and performs no R2, RunPod,
D1, or paid external-resource operation.

## License

Apache 2.0 — see [LICENSE](./LICENSE).
