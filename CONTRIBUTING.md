# Contributing to ComfyComplete

Thank you for your interest in contributing to ComfyComplete! This guide covers the essentials for adding custom node packs, updating dependencies, and submitting pull requests.

## Overview

ComfyComplete is a curated ComfyUI distribution. The primary configuration lives in `supported_nodes.yaml`, with Python dependencies pinned in `requirements.txt`.

Before contributing, read [The 10 Rules](docs/adding-custom-nodes.md#the-10-rules) in the custom nodes guide. These are non-negotiable requirements that apply to every submission.

## Adding a Custom Node Pack

1. Add an entry to `supported_nodes.yaml` with the required fields (repository URL or registry name, version, and labels).
2. Add any new Python dependencies to `dependency_overrides` in your YAML entry.
3. Include test workflows in `tests/node-tests/<pack-name>/`.
4. Run the test suite to validate your changes.

For the full YAML format reference, label definitions, and the complete onboarding process, see [docs/adding-custom-nodes.md](docs/adding-custom-nodes.md).

## Eligibility

Not every node pack will be accepted. Priority goes to packs with 1,000+ downloads on the Comfy Registry. We reserve the right to reject any node for reasons including dependency conflicts, functionality duplication, license incompatibility, or strategic considerations. See the [eligibility criteria](docs/adding-custom-nodes.md#eligibility-criteria) for details.

## Updating Dependencies

All Python dependencies in `requirements.txt` must be pinned to exact versions using `==`. Unpinned or loosely pinned dependencies (e.g., `>=`, `~=`) will be rejected by CI.

Example:
```
torch==2.5.1
numpy==1.26.4
```

### dependency_overrides

If your node pack needs specific package versions, declare them in the `dependency_overrides` field of your YAML entry rather than editing `requirements.txt` directly. The `scripts/extract_deps.py` tool helps maintainers identify conflicts and merge overrides.

```yaml
node_packs:
  - name: comfyui-my-nodes
    version: "1.0.0"
    dependency_overrides:
      - "special-lib==2.3.1"
      - "another-dep==0.5.0"
```

See [dependency_overrides vs requirements.txt](docs/adding-custom-nodes.md#dependency_overrides-vs-requirementstxt) for a full explanation.

## Models

If your node pack requires pre-trained models, declare them in the `models:` field. Do not download models at runtime -- this will result in the `RuntimeModelDownload` label and your node being disabled on cloud.

```yaml
node_packs:
  - name: comfyui-my-nodes
    version: "1.0.0"
    models:
      - name: "my-model.safetensors"
        url: "https://huggingface.co/user/model/resolve/main/my-model.safetensors"
        directory: "checkpoints"
```

See [Models Declaration](docs/adding-custom-nodes.md#models-declaration) for the complete format and how models are handled.

## Labels

Labels in `supported_nodes.yaml` control how node packs are filtered and deployed across environments. Every node that reads files, writes to disk, accesses the network, or has other special behaviors must be labeled. See [docs/adding-custom-nodes.md](docs/adding-custom-nodes.md) for the full list of available labels and their meanings.

## Test Workflows

Every node pack submission must include test workflows:

- Place them in `tests/node-tests/<pack-name>/`
- Use ComfyUI API-format JSON (Enable Dev Mode in ComfyUI, then Save (API Format))
- Cover every node that is not disabled by labels
- Each workflow file must be valid JSON with `class_type` and `inputs` on every node

For a complete guide on writing test workflows, see [docs/test-workflow-guide.md](docs/test-workflow-guide.md).

## Running Tests

```bash
pip install pytest pyyaml uv
pytest tests/ -v
```

Tests validate YAML structure, dependency pinning, label correctness, and configuration consistency.

To validate test workflows specifically:

```bash
python scripts/validate_test_workflows.py
```

## PR Process

### Step-by-step

1. Fork the repository and create a feature branch.
2. Add your node pack entry to `supported_nodes.yaml` with labels.
3. Add test workflows to `tests/node-tests/<pack-name>/`.
4. Declare any dependency overrides and model requirements in the YAML entry.
5. Ensure all tests pass locally (`pytest tests/ -v`).
6. Open a pull request.

### What happens after you submit

1. **CI Validation** -- Automated checks verify YAML structure, label declarations, dependency pinning, and test workflow format.
2. **Review Agent** -- An automated agent clones your node pack, runs security scans, analyzes the code, and verifies labels match actual behavior.
3. **Ephemeral Testing** -- A maintainer triggers a test environment that builds a container with your nodes and runs your test workflows.
4. **QA Sign-Off** -- A maintainer reviews all findings and approves or requests changes.
5. **Merge** -- Once approved, the PR is merged and CI rebuilds the distribution.

## Code of Conduct

Be respectful and constructive. If the automated reviewer flags something you believe is safe, explain your reasoning in the PR comments rather than dismissing the finding.
