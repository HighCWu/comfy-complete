# Cloud Node Integration Guide

How custom nodes flow from configuration to production in Comfy Cloud.

## Architecture Overview

```
comfy-complete/                          Cloud-specific
├── supported_nodes.yaml  ─────────────► comfyui.Dockerfile (install nodes)
├── requirements.txt      ─────────────► comfyui.Dockerfile (pip install)
├── version_lock.yaml     ─────────────► comfyui.Dockerfile (checkout ComfyUI ref)
├── cloud_overlay.yaml    ─────────────► comfyui.Dockerfile (install cloud-only nodes)
├── cloud_disable_config.yaml ─────────► filter_object_info_nodes.py (filter nodes)
└── scripts/
    ├── install_custom_nodes.py ───────► comfyui.Dockerfile (node installation)
    └── resolve_disabled_nodes.py ─────► filter_object_info_nodes.py (label resolution)

services/inference/
├── custom_node_patches/   ────────────► post_install_nodes.py (apply after install)
└── scripts/post_install_nodes.py ─────► comfyui.Dockerfile (patches + fixups)

scripts/
├── filter_object_info_nodes.py ───────► sync-objectinfo.yml (CI: filter object_info)
└── sync_custom_node_extensions.py ────► sync-objectinfo.yml (CI: copy web assets)
```

## How Node Filtering Works

Nodes are disabled on cloud through a **label-based system**:

1. `supported_nodes.yaml` assigns labels to individual nodes (e.g., `ReadsArbitraryFile`, `DisabledOnCloud`)
2. `cloud_disable_config.yaml` declares which labels cause filtering (OR logic — any matching label disables the node)
3. `resolve_disabled_nodes.py` combines these to produce the full disable list
4. `filter_object_info_nodes.py` removes disabled nodes from `object_info.json`

**Key distinction**: Adding a label to `supported_nodes.yaml` does NOT automatically disable that node on cloud. The label must also appear in `cloud_disable_config.yaml`.

## Adding a Node Pack

### Registry Node (published to comfy.org)

Edit `comfy-complete/supported_nodes.yaml`:

```yaml
node_packs:
  - name: comfyui-example-nodes
    version: "2.1.0"
```

If nodes need labels:

```yaml
  - name: comfyui-example-nodes
    version: "2.1.0"
    node_labels:
      LoadFromPath:
        - ReadsArbitraryFile
      SaveToFile:
        - WritesToDisk
```

### Git-Pinned Node (not in registry)

Use the full GitHub URL with a commit SHA:

```yaml
  - name: "https://github.com/user/ComfyUI-Example@abc123def456"
    version: ""
```

### Node with Web Extensions

If the node has a non-standard web directory (not `web/`), specify it:

```yaml
  - name: comfyui-example-nodes
    version: "1.0.0"
    web_directory: js
```

The `sync-objectinfo.yml` CI workflow copies web assets from this directory to `services/ingest/static/extensions/<node-name>/`.

### Node with Custom Patches

If a node needs cloud-specific modifications (e.g., removing features, locking settings):

1. Create a patch directory: `services/inference/custom_node_patches/<node-name>/`
2. Add numbered patch files: `001-description.patch`, `002-description.patch`
3. Patches are applied by `post_install_nodes.py` after node installation

Generate patches:
```bash
cd /path/to/installed/custom_nodes/<node-name>
# Make your changes
git diff > /path/to/cloud/services/inference/custom_node_patches/<node-name>/001-description.patch
```

The patch system handles both git-cloned nodes (uses `git apply`) and registry-installed nodes (falls back to `patch -p1`).

### Cloud-Only Node

Nodes that should only exist in the cloud sidecar (not in the open-source comfy-complete distribution) go in `cloud_overlay.yaml`:

```yaml
node_packs:
  - name: comfyui-cloud-monitoring
    version: "1.0.0"
```

## Real Examples (Before → After)

### Example 1: Git-Pinned Node with Disabled Nodes

**Before** (old `common/supported_custom_nodes.json`):
```json
{
  "name": "https://github.com/spacepxl/ComfyUI-Image-Filters@bbb3fb00...",
  "version": "",
  "disallow_nodes": ["ModelTest"]
}
```

**After** (`comfy-complete/supported_nodes.yaml`):
```yaml
- name: "https://github.com/spacepxl/ComfyUI-Image-Filters@bbb3fb00..."
  version: ""
  node_labels:
    ModelTest:
      - DisabledOnCloud
```

### Example 2: Registry Node with Patches

**Before** (`common/supported_custom_nodes.json` + inline patch logic):
```json
{
  "name": "ComfyUI-QwenVL",
  "version": "2.1.1",
  "disallow_nodes": ["AILab_QwenVL_GGUF", "AILab_QwenVL_GGUF_Advanced"]
}
```
Patches were applied inline by the old `services/inference/install_custom_nodes.py`.

**After** (`comfy-complete/supported_nodes.yaml` + separate patch files):
```yaml
- name: ComfyUI-QwenVL
  version: "2.1.1"
  node_labels:
    AILab_QwenVL_GGUF:
      - DisabledOnCloud
    AILab_QwenVL_GGUF_Advanced:
      - DisabledOnCloud
```
Patches live in `services/inference/custom_node_patches/ComfyUI-QwenVL/` and are applied by `post_install_nodes.py`.

### Example 3: Git-Pinned Node, No Labels, No Patches

**Before**:
```json
{
  "name": "https://github.com/shiimizu/ComfyUI-TiledDiffusion@a155b1ba...",
  "version": ""
}
```

**After**:
```yaml
- name: "https://github.com/shiimizu/ComfyUI-TiledDiffusion@a155b1ba..."
  version: ""
```

## CI Pipeline

When a PR modifies `comfy-complete/` files:

1. **Cloud Build** builds the sidecar Docker image (`comfyui.Dockerfile`)
2. **`sync-objectinfo.yml`** waits for the sidecar image, then:
   - Starts ComfyUI in the container
   - Fetches `/object_info` from the running instance
   - Filters disabled nodes via `filter_object_info_nodes.py`
   - Extracts API-only nodes for no-GPU billing
   - Copies web extensions from the container
   - Commits updated `object_info.json` and extensions back to the PR

Files auto-updated by CI (do not edit manually):
- `services/ingest/data/object_info.json`
- `services/ingest/static/extensions/`
- `services/dispatcher/server/services/preprocessing/data/no_gpu_nodes.json`

## Files Reference

| File | Purpose |
|------|---------|
| `comfy-complete/supported_nodes.yaml` | Node pack definitions, versions, and labels |
| `comfy-complete/requirements.txt` | Pinned Python dependencies for all nodes |
| `comfy-complete/version_lock.yaml` | ComfyUI core ref pin |
| `comfy-complete/cloud_overlay.yaml` | Cloud-only nodes (not open-source) |
| `comfy-complete/cloud_disable_config.yaml` | Which labels trigger node filtering |
| `comfy-complete/scripts/install_custom_nodes.py` | Installs nodes via comfy-cli |
| `comfy-complete/scripts/resolve_disabled_nodes.py` | Resolves label config → disabled node list |
| `services/inference/custom_node_patches/` | Per-node cloud patches |
| `services/inference/scripts/post_install_nodes.py` | Applies patches + post-install fixups |
| `scripts/filter_object_info_nodes.py` | Filters object_info.json (CI script) |
| `scripts/sync_custom_node_extensions.py` | Copies web extensions (CI script) |

## Checklist for Adding a Node

- [ ] Entry added to `comfy-complete/supported_nodes.yaml`
- [ ] Labels added for nodes that read files, write to disk, access network, etc.
- [ ] Labels use only declared label names (run `resolve_disabled_nodes.py --validate`)
- [ ] New Python dependencies added to `comfy-complete/requirements.txt` (pinned, exact versions)
- [ ] Cloud patches created in `services/inference/custom_node_patches/` if needed
- [ ] Models added to `common/supported_models.json` if needed
- [ ] PR triggers CI, which auto-generates `object_info.json` and web extensions
