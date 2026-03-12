# Adding Custom Nodes to Comfy Complete

This guide explains how to add or update custom node packs in comfy-complete.

## The 10 Rules

Before submitting, review every rule. Violating any of these will delay or block your PR.

1. **Check your license FIRST.** InsightFace, AGPL, or any license incompatible with redistribution means instant rejection. If your node bundles or depends on a restrictively-licensed library, it cannot be included.

2. **Save nodes MUST populate `{"ui": {"images": [...]}}`.**  Managed deployments upload outputs based on this return value. If your save/preview node returns an empty dict, outputs may never reach the user.

3. **Keep dependencies minimal.** Zero external dependencies means 100% installation success. Every dependency you add is a potential version conflict with the 60+ other node packs in the distribution. Pin to exact versions.

4. **No runtime model downloads.** Declare all required models in the `models:` field of your YAML entry. Managed deployments pre-provision models; downloading at inference time adds latency and may be blocked by network policy. Nodes that download models at runtime receive the `RuntimeModelDownload` label.

5. **No runtime pip installs.** Nodes that call `pip install` or `subprocess.run(["pip", ...])` during execution will receive the `RuntimePipInstall` label. Declare all dependencies upfront in `dependency_overrides` or coordinate with maintainers to add them to `requirements.txt`.

6. **Custom endpoints may not work.** Managed deployments proxy a subset of ComfyUI's HTTP API. Custom `@routes` decorators or `PromptServer` route registrations may not be reachable. Nodes with custom endpoints receive the `HasCustomEndpoints` label.

7. **Custom widget types are risky.** Non-standard input/output types that require custom frontend JavaScript may not render correctly in all frontends. Stick to standard ComfyUI types (IMAGE, MASK, LATENT, STRING, INT, FLOAT, COMBO, etc.) when possible.

8. **Interactive canvas widgets may break.** Nodes that open browser popups, create interactive overlays, or require direct DOM manipulation may not function in headless/managed environments. These will receive the `RequiresDisplay` label.

9. **File I/O nodes need labels.** Any node that reads from arbitrary file paths gets `ReadsArbitraryFile`. Any node that writes to disk gets `WritesToDisk`. Labels are CI gates — the automated reviewer will flag missing labels.

10. **Non-standard output formats (.exr, .hdr, .ply, .glb) need coordination.** If your node produces files in formats not already supported by the distribution's output handling, flag this in your PR description so maintainers can coordinate.

## Eligibility Criteria

Not all node packs will be accepted. The following criteria determine review priority:

- **1,000+ downloads on the Comfy Registry** = prioritized review. High-download node packs demonstrate community demand and are reviewed first.
- **We reserve the right to reject any node, even if technically correct.** Reasons include but are not limited to: breaks existing dependencies, duplicates functionality already provided by an included pack, or excessive maintenance burden.
- **License compatibility is mandatory.** GPL, AGPL, and other copyleft licenses that impose redistribution restrictions are not compatible with the distribution model.

## YAML Format Reference

### Minimal Entry (Registry Node)

```yaml
node_packs:
  - name: comfyui-example-nodes
    version: "1.0.0"
```

### Minimal Entry (GitHub-Pinned Node)

```yaml
node_packs:
  - name: "https://github.com/username/repo@full-commit-sha"
    version: ""
```

### Full Entry (All Fields)

```yaml
node_packs:
  - name: comfyui-example-nodes
    version: "2.1.0"
    web_directory: js                    # Only if non-standard (not "web/")
    node_labels:
      LoadFromPath:
        - ReadsArbitraryFile
      SaveToFile:
        - WritesToDisk
      FetchFromURL:
        - NetworkAccess
      DownloadModel:
        - RuntimeModelDownload
        - NetworkAccess
    dependency_overrides:
      - "some-special-lib==1.2.3"        # Overrides or adds to requirements.txt
      - "another-lib==0.9.1"
    models:
      - name: "big-lama.pt"
        url: "https://github.com/advimman/lama/releases/download/v1.0/big-lama.pt"
        directory: "inpaint"
      - name: "model-v2.safetensors"
        url: "https://huggingface.co/user/model/resolve/main/model-v2.safetensors"
        directory: "checkpoints"
        filename: "custom-name.safetensors"   # Optional: rename on download
```

### Field Reference

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Registry package name or full GitHub URL with `@commit-sha` |
| `version` | No | Registry version string. Use `""` for git-pinned nodes |
| `web_directory` | No | Custom web extension directory name (only if not `web/`) |
| `node_labels` | No | Map of node class names to lists of label strings |
| `dependency_overrides` | No | List of pip requirement strings to add/override |
| `system_dependencies` | No | List of system packages (apt) needed at build time |
| `models` | No | List of model declarations (see Models section) |

## Models Declaration

If your node pack requires pre-trained models, declare them in the `models:` field instead of downloading them at runtime.

### Model Entry Format

```yaml
models:
  - name: "model-file.safetensors"      # Name/identifier for the model
    url: "https://example.com/model.safetensors"  # Direct download URL
    directory: "checkpoints"             # ComfyUI model subdirectory
    filename: "renamed.safetensors"      # Optional: filename on disk (defaults to name)
```

### What Happens with Declared Models

- During container builds, declared models are registered in the model management system.
- At inference time, models are downloaded on-demand and cached locally.
- The `url` field is used for initial ingestion only.
- Models in `directory` are placed under ComfyUI's `models/<directory>/` path.

### Model Directory Reference

Common directories: `checkpoints`, `clip`, `controlnet`, `diffusers`, `embeddings`, `loras`, `upscale_models`, `vae`, `inpaint`, `sams`, `detection`.

## dependency_overrides vs requirements.txt

The `requirements.txt` file contains all Python dependencies for the entire distribution, pinned to exact versions. It is the single source of truth for what gets installed.

`dependency_overrides` in `supported_nodes.yaml` is a declaration mechanism:

- It tells maintainers "this node pack needs these specific package versions."
- The `scripts/extract_deps.py` tool compares overrides against `requirements.txt` and reports new deps, conflicts, and resolution suggestions.
- Overrides do NOT automatically modify `requirements.txt`. A maintainer must review and apply them.
- If your override conflicts with an existing pin, the maintainer will work with you to find a compatible version.

When to use `dependency_overrides`:
- Your node needs a package not already in `requirements.txt`
- Your node needs a different version of an existing package
- Your node has a build-time dependency (e.g., a specific CUDA version of a library)

## Available Labels

Labels describe what a node **does**. Each deployment chooses which labels to restrict via its own policy file.

| Label | When Required |
|-------|---------------|
| `ReadsArbitraryFile` | Node accepts a file path input and reads from it |
| `WritesToDisk` | Node writes files to the filesystem |
| `CreatesLargeOutputs` | Node produces large files (video, audio, models) |
| `NetworkAccess` | Node makes HTTP/network requests |
| `RequiresExternalAPI` | Node requires external API keys (OpenAI, Anthropic, etc.) |
| `Stateful` | Node persists user-specific data between workflow runs |
| `HasCustomEndpoints` | Node registers custom HTTP server routes |
| `PathParsing` | Node exposes filesystem path information |
| `DuplicateOfCoreNode` | Node duplicates functionality of a core ComfyUI node |
| `Incompatible` | Node is incompatible with the distribution environment |
| `RequiresWebcam` | Node requires webcam hardware access |
| `RequiresDisplay` | Node requires interactive display or browser UI |
| `RequiresClipboard` | Node requires system clipboard access |
| `RequiresGPU` | Node hardcodes CUDA/GPU and will crash without one |
| `BrokenNode` | Node is currently broken or non-functional |
| `ExecutesArbitraryCode` | Node executes user-provided code (eval, exec, pickle) |
| `RuntimeModelDownload` | Node downloads models from the internet at execution time |
| `RuntimePipInstall` | Node installs Python packages via pip at execution time |

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

## Validate Locally (Before Submitting)

Run these scripts locally to catch issues before CI does. All scripts are in `scripts/add-node/`.

### Quick Check (all-in-one)

```bash
# Clone your node, run security scan + label suggestions
./scripts/add-node/check-node.sh https://github.com/you/ComfyUI-MyNodes

# If you've already added your entry to supported_nodes.yaml, also validate it
./scripts/add-node/check-node.sh https://github.com/you/ComfyUI-MyNodes --name comfyui-mynodes
```

### Individual Scripts

**Security scan** — checks for dangerous code patterns (blockers + warnings):
```bash
./scripts/pr-review/security-scan.sh /path/to/your/node/repo
```

**Label suggestions** — analyzes your code and suggests which labels to declare:
```bash
python scripts/add-node/suggest-labels.py /path/to/your/node/repo
```

Output includes a copy-paste YAML block for `node_labels`. The script checks for 15 of 18 labels automatically (3 require human judgment).

**License check** — verifies that the node and its dependencies have compatible licenses:
```bash
python scripts/add-node/check-license.py /path/to/your/node/repo
```

Checks the repository license file and scans installed dependencies for license compatibility. Flags AGPL, GPL, and other copyleft licenses that are incompatible with redistribution. Reports blocked, unknown, and permissive licenses.

**Entry validation** — validates your `supported_nodes.yaml` entry:
```bash
python scripts/add-node/validate-entry.py --yaml supported_nodes.yaml --name comfyui-mynodes
```

Checks: registry name exists, version format, labels are declared, no duplicates, dependency format, model fields.

**Dependency check** — compares your overrides against existing requirements:
```bash
python scripts/extract_deps.py --yaml supported_nodes.yaml --requirements requirements.txt
```

### Machine-Readable Output

All Python scripts support `--json` for CI integration:
```bash
python scripts/add-node/suggest-labels.py /path/to/repo --json
python scripts/add-node/validate-entry.py --yaml supported_nodes.yaml --name comfyui-mynodes --json
```

## Test Workflows

Every new node pack submission must include test workflows. See [test-workflow-guide.md](test-workflow-guide.md) for the full guide.

Key points:
- Place test workflows in `tests/node-tests/<pack-name>/`
- Use ComfyUI API-format JSON (not the default Save format)
- Every node that is NOT labeled should have test coverage
- Each workflow must be valid JSON with `class_type` and `inputs` on every node

## Submission Flow

### Step 1: Fork and Branch

Fork the repo, create a branch, and add your entry to `supported_nodes.yaml`.

### Step 2: Run Local Checks

```bash
./scripts/add-node/check-node.sh https://github.com/you/ComfyUI-MyNodes --name comfyui-mynodes
```

Fix any issues the scripts flag before submitting.

### Step 3: Submit PR

Submit a PR with:
- YAML entry in `supported_nodes.yaml` with all required fields and labels
- Test workflows in `tests/node-tests/<pack-name>/`
- Any new Python dependencies documented in `dependency_overrides`
- Model declarations in `models:` if applicable

### Step 4: CI Validation

Automated checks verify:
- YAML syntax and structure
- All labels are from the declared set
- All dependencies are pinned to exact versions
- Test workflows are valid API-format JSON
- Security scan passes (no blockers)

### Step 5: Automated Review

An automated review workflow will:
1. Clone your node pack
2. Run security scans for dangerous patterns
3. Verify declared labels match actual node behavior
4. Check for missing labels
5. Post a review comment with findings

### Step 6: Maintainer Review

A maintainer reviews:
- Automated review findings
- Test workflow coverage
- Dependency impact on the broader distribution
- Any manual testing needed for interactive or visual nodes

### Step 7: Merge

Once all checks pass and a maintainer approves, the PR is merged.

## Deployment Compatibility

The reviewer checks for deployment issues:

| Check | Impact |
|-------|--------|
| **Stateful nodes** | Nodes that persist user-specific data between runs may not work correctly in managed environments |
| **Custom UI** | `web/` or `js/` directories that modify the interface |
| **Custom endpoints** | `@routes` decorators that register HTTP endpoints |
| **External APIs** | Nodes requiring OpenAI, Anthropic, or other API keys |
| **System packages** | Dependencies on espeak, tesseract, or other system packages not in the base image |
| **Runtime downloads** | Nodes that download models or pip-install packages during execution |

## Questions?

If the reviewer flags something you believe is safe, explain your reasoning in the PR comments. The review is automated but humans make the final decision.
