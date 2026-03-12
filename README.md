# Comfy Complete

A batteries-included distribution of ComfyUI, open-sourced for the community.

## What is Comfy Complete?

Comfy Complete provides:

- **Pinned Python Dependencies**: All dependencies are pinned to exact versions that are tested to work together
- **Curated Custom Nodes**: A set of popular custom nodes pre-configured and tested for compatibility
- **Version-Locked ComfyUI**: Specific commit of ComfyUI core that's known to work with all included dependencies

This makes it easy to:
- Run ComfyUI locally without dependency conflicts
- Ensure your custom node works with the broader ecosystem
- Prepare your node pack for inclusion in managed ComfyUI deployments

## Quick Start

### Using Docker Compose (Recommended)

Update the volumn in `compose.yaml` with your `/path/to/models`, and run:

```bash
# Run ComfyUI
docker compose up -d

# Stop ComfyUI
docker compose down
```

### Manual Installation

1. Clone ComfyUI at the pinned version:
```bash
git clone https://github.com/Comfy-Org/ComfyUI.git
cd ComfyUI
git checkout $(grep -A1 'comfyui:' /path/to/comfy-complete/version_lock.yaml | grep ref | cut -d'"' -f2)
```

2. Install Python dependencies:
```bash
pip install -r /path/to/comfy-complete/requirements.txt
```

3. Install custom nodes:
```bash
pip install comfy-cli pyyaml
python /path/to/comfy-complete/scripts/install_custom_nodes.py --comfy-path .
```

## Repository Structure

```
comfy-complete/
├── compose.yaml            # Compose file
├── requirements.txt        # Pinned Python dependencies
├── supported_nodes.yaml    # Supported custom node packs with versions
├── version_lock.yaml       # ComfyUI core version pinning
├── scripts/
│   └── install_custom_nodes.py  # Custom node installation script
├── docker/
│   ├── Dockerfile          # Container build file
│   └── entrypoint.sh       # Container entrypoint
└── tests/
    └── test_requirements.py     # Dependency validation tests
```

## For Custom Node Authors

### Making Your Node Compatible

1. **Target Comfy Complete Dependencies**: Check `requirements.txt` for available packages and their versions. If your node needs these packages, it should work with the listed versions.

2. **Specify Additional Dependencies**: If your node requires packages not in Comfy Complete, list them in your `requirements.txt`. Include transitive dependencies with pinned versions.

3. **Avoid Conflicting Versions**: Don't require versions that conflict with Comfy Complete's pinned versions.

### Getting Your Node Added

To add your custom node to Comfy Complete:

1. Ensure your node works with the dependencies in `requirements.txt`
2. Add any additional required dependencies to your node's `requirements.txt`
3. Submit a PR adding your node to `supported_nodes.yaml`
4. An automated reviewer will analyze your code for security and labeling

**See [docs/adding-custom-nodes.md](docs/adding-custom-nodes.md) for the complete guide.**

### Node Pack Configuration

Nodes in `supported_nodes.yaml` may have:

- `version`: Pinned version or git commit
- `node_labels`: Permission labels for specific nodes
- `web_directory`: Custom web assets directory

Example:
```yaml
- name: my-custom-nodes
  version: "1.0.0"
  node_labels:
    LoadFromPath:
      - ReadsArbitraryFile
    SaveToFile:
      - WritesToDisk
```

### Available Labels

Labels describe what a node **does** — each deployment decides which labels to disable via its own policy file.

| Label                  | Description                                                  |
|------------------------|--------------------------------------------------------------|
| `ReadsArbitraryFile`   | Node reads from user-provided file paths                     |
| `WritesToDisk`         | Node writes files to filesystem                              |
| `CreatesLargeOutputs`  | Node produces large outputs (video, audio, models)           |
| `NetworkAccess`        | Node makes network requests                                  |
| `RequiresExternalAPI`  | Node requires external API keys                              |
| `Stateful`             | Node persists user-specific data between runs                |
| `HasCustomEndpoints`   | Node registers custom HTTP server routes                     |
| `PathParsing`          | Node exposes filesystem path information                     |
| `DuplicateOfCoreNode`  | Node duplicates functionality of a core ComfyUI node         |
| `Incompatible`         | Node is incompatible with the distribution environment       |
| `RequiresWebcam`       | Node requires webcam hardware access                         |
| `RequiresDisplay`      | Node requires interactive display or browser UI              |
| `RequiresClipboard`    | Node requires system clipboard access                        |
| `RequiresGPU`          | Node hardcodes CUDA/GPU usage and will crash without one     |
| `BrokenNode`           | Node is currently broken or non-functional                   |
| `ExecutesArbitraryCode`| Node executes user-provided code (eval, exec, pickle, etc.) |
| `RuntimeModelDownload` | Node downloads models from the internet at execution time    |
| `RuntimePipInstall`    | Node installs Python packages via pip at execution time      |

## Testing

Run the test suite to validate the environment:

```bash
# Install test dependencies
pip install pytest pyyaml

# Run tests
pytest tests/ -v
```

The tests verify:
- `requirements.txt` is valid and resolvable
- All packages are pinned to exact versions
- YAML config files are valid
- No known conflicting packages

## Configuration Files

### version_lock.yaml

Pins the exact version of ComfyUI core:

```yaml
pinned:
  comfyui:
    ref: "<ref>"
  comfyui_frontend:
    ref: "<ref>"
  workflow_templates:
    ref: "<ref>"
```

### supported_nodes.yaml

Lists all supported custom node packs:

```yaml
node_packs:
  - name: comfyui-kjnodes
    version: "1.1.6"
    node_labels:
      LoadVideosFromFolder:
        - ReadsArbitraryFile
```

## Docker Environment Variables

| Variable               | Default   | Description                                        |
|------------------------|-----------|----------------------------------------------------|
| `COMFY_LISTEN_HOST`    | `0.0.0.0` | Host to listen on                                  |
| `COMFY_PORT`           | `8188`    | Port to listen on                                  |
| `COMFY_PREVIEW_METHOD` | (none)    | Preview method (latent2rgb, taesd, etc.)           |
| `COMFY_EXTRA_ARGS`     | (none)    | Additional ComfyUI arguments                       |
| `COMFY_EXTRA_LIBS`     | (none)    | Additional ComfyUI dependencies (for testing ONLY) |

## FAQ

**Q: Does this solve all local dependency issues?**

Partially. This sets a baseline of dependencies that work together. If you use only nodes from Comfy Complete, you shouldn't have conflicts.

**Q: Can I use any node pack with Comfy Complete?**

Nodes not in `supported_nodes.yaml` may work but aren't guaranteed. They might require dependencies that conflict with the pinned versions.

**Q: How do I update to a newer version of a node pack?**

Submit a PR updating the version in `supported_nodes.yaml`. Ensure tests pass with the new version.

**Q: My node pack was removed. Why?**

Node packs may be removed if they become difficult to maintain or conflict with more commonly used packages.

## Contributing

Contributions are welcome! Please:

1. Test changes locally with the full test suite
2. Ensure dependency changes don't break existing node packs
3. Follow the existing code style
4. Update documentation as needed

## License

[License information here]
