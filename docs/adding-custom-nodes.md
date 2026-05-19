# Adding Custom Nodes to Comfy Complete

This guide explains how to add or update custom node packs in comfy-complete.

## Overview

When you submit a PR that modifies `supported_nodes.yaml`, an automated review agent will analyze your changes and provide feedback on:

- **Security** - Checking for dangerous code patterns
- **Labels** - Ensuring nodes have correct permission labels

## How to Add a Node Pack

### 1. Add Entry to `supported_nodes.yaml`

**For registry nodes** (published to [comfy.org](https://comfy.org)):

```yaml
node_packs:
  - name: comfyui-example-nodes
    version: "1.0.0"
```

**For GitHub-hosted nodes** (not in registry):

```yaml
node_packs:
  - name: "https://github.com/username/repo@commitsha"
    version: ""
```

### 2. Add Permission Labels

If any nodes in the pack require special permissions, declare them:

```yaml
node_packs:
  - name: comfyui-example-nodes
    version: "1.0.0"
    node_labels:
      LoadFromPath:
        - ReadsArbitraryFile
      SaveToFile:
        - WritesToDisk
```

### 3. Submit PR

The automated reviewer will:

1. Clone your node pack
2. Run security scans
3. Read and analyze the code
4. Verify labels are correct
5. Post a review comment

## Available Labels

| Label | When Required |
|-------|---------------|
| `ReadsArbitraryFile` | Node accepts a file path input and reads from it |
| `WritesToDisk` | Node writes files to the filesystem |
| `NetworkAccess` | Node makes HTTP/network requests |
| `CreatesLargeOutputs` | Node produces large files (video, audio, batch images) |
| `DisabledOnCloud` | Node won't work in cloud deployments (hardware access, GUI, etc.) |
| `Stateful` | Node persists user-specific data between workflow runs |
| `HasCustomEndpoints` | Node registers custom HTTP server routes |
| `RequiresExternalAPI` | Node requires external API keys (OpenAI, Anthropic, etc.) |

## Security Requirements

The following patterns will **block** your PR:

| Pattern | Risk | Solution |
|---------|------|----------|
| `eval()` | Arbitrary code execution | Refactor to avoid dynamic code execution |
| `exec()` | Arbitrary code execution | Use safer alternatives |
| `os.system()` | Command injection | Use `subprocess.run()` with list args |
| `shell=True` | Shell injection | Use `shell=False` with list args |
| `pickle.load/loads` | Code execution via deserialization | Use safer formats (JSON, safetensors) |
| `torch.load()` without `weights_only=True` | Pickle vulnerability | Add `weights_only=True` parameter |

## Cloud Compatibility

The reviewer also checks for cloud deployment issues:

| Check | Impact |
|-------|--------|
| **Stateful nodes** | Nodes that persist user-specific data between runs may not work correctly in cloud |
| **Custom UI** | `web/` or `js/` directories that modify the interface without user consent |
| **Custom endpoints** | `@routes` decorators that register stateful HTTP endpoints |
| **External APIs** | Nodes requiring OpenAI, Anthropic, or other API keys |
| **System packages** | Dependencies on espeak, tesseract, or other system packages not in cloud |

**Important**: Verify your node doesn't have:
- "Stateful" nodes that persist data between runs
- UI changes that modify the default interface without user consent

## What the Reviewer Checks

### Automated Scan
- Pattern-based detection of dangerous functions
- File write operations
- Network access patterns
- Statefulness patterns (caches, singletons, global state)
- Custom UI directories
- Custom server endpoints
- External API dependencies

### Code Analysis
The reviewer reads your actual code to:
- Understand what each node does
- Find vulnerabilities the scan might miss
- Verify declared labels match actual behavior
- Identify stateful patterns that could cause issues
- Check if custom UI modifies the interface appropriately

## Example PR

```yaml
# Adding a new image utility pack
node_packs:
  - name: comfyui-image-utils
    version: "2.1.0"
    node_labels:
      LoadImageFromPath:
        - ReadsArbitraryFile
      SaveImageToPath:
        - WritesToDisk
      FetchImageFromURL:
        - NetworkAccess
```

## Questions?

If the reviewer flags something you believe is safe, explain your reasoning in the PR comments. The review is automated but humans make the final decision.
