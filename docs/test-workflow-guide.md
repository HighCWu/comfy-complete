# Test Workflow Guide

This guide explains how to create, structure, and maintain test workflows for custom node packs in Comfy Complete.

## What Are Test Workflows?

Test workflows are ComfyUI API-format JSON files that exercise individual nodes or chains of nodes from a custom node pack. They are run during ephemeral testing to verify that nodes install correctly, load without errors, and produce valid outputs.

## File Format

Test workflows use the **ComfyUI API format**, which is different from the default workflow save format. The API format is a flat dictionary mapping string node IDs to node configurations.

Each node configuration must have:
- `class_type` -- the registered node class name
- `inputs` -- a dictionary of input values

Connections between nodes are expressed as `[node_id, output_index]` tuples in the inputs.

### Example: Minimal Test Workflow

```json
{
  "1": {
    "class_type": "TestDefinition",
    "inputs": {
      "name": "MyNodeTest",
      "description": "Test basic functionality of MyNode",
      "requiresGPU": false,
      "extraTime": 0
    }
  },
  "2": {
    "class_type": "TestImageGenerator",
    "inputs": {
      "image_type": "noise",
      "width": 256,
      "height": 256,
      "seed": 42
    }
  },
  "3": {
    "class_type": "MyCustomNode",
    "inputs": {
      "image": ["2", 0],
      "strength": 0.5,
      "mode": "default"
    }
  },
  "4": {
    "class_type": "AssertExecuted",
    "inputs": {
      "input": ["3", 0]
    }
  }
}
```

### Structure Explained

- **Node "1" (TestDefinition)** -- Metadata about the test. The `name` field identifies the test in results. Set `requiresGPU` to `true` if the node needs GPU hardware.
- **Node "2" (TestImageGenerator)** -- Generates a synthetic input image so the test does not depend on external files.
- **Node "3" (MyCustomNode)** -- The node being tested. Receives input from node 2.
- **Node "4" (AssertExecuted)** -- Verifies that node 3 ran without error. If node 3 fails, the assertion catches it.

## How to Export from ComfyUI

1. Open ComfyUI and build a workflow that exercises your node.
2. Go to **Settings** and enable **Dev Mode** (under the Developer section).
3. Click **Save (API Format)** in the menu. This saves the workflow in the flat API format required for tests.
4. Rename the file following the naming convention: `test_<description>.json`.
5. Place it in the correct directory (see below).

**Important:** The default "Save" button produces the full workflow format (with `links`, `nodes` arrays, and UI metadata). This format is NOT valid for test workflows. You must use "Save (API Format)".

## Where to Put Test Workflows

```
tests/node-tests/
  <pack-name>/
    test_basic_operation.json
    test_edge_case.json
    test_multiple_nodes.json
```

The `<pack-name>` directory must match the node pack's `name` field in `supported_nodes.yaml`. For git-pinned packs (URL format), use the repository name portion:

| YAML name | Test directory |
|-----------|----------------|
| `comfyui-kjnodes` | `tests/node-tests/comfyui-kjnodes/` |
| `comfyui_essentials` | `tests/node-tests/comfyui_essentials/` |
| `https://github.com/user/ComfyUI-Example@abc123` | `tests/node-tests/ComfyUI-Example/` |

## Coverage Requirements

### What Needs Tests

Every node class that is NOT disabled by labels should have at least one test workflow covering it. "Disabled by labels" means the node has a label that causes it to be filtered out by the deployment's disable config (e.g., `RuntimeModelDownload`, `Incompatible`, `RequiresDisplay`).

### What Does NOT Need Tests

- Nodes with the `Incompatible` label
- Nodes with the `RequiresDisplay` label (interactive UI nodes)
- Nodes with the `RequiresWebcam` label
- Nodes with the `RuntimeModelDownload` label (unless models are declared in `models:`)
- Nodes with the `BrokenNode` label

### Coverage Strategy

You do not need one test file per node. A single workflow can test multiple nodes by chaining them together. For example, a workflow that loads an image, applies a filter, and saves the result tests three nodes at once.

## Chain Workflows

Chain workflows test multiple nodes in a single file by connecting them in sequence. This is the recommended approach for node packs with many related nodes.

### Example: Chain Workflow Testing Three Nodes

```json
{
  "1": {
    "class_type": "TestDefinition",
    "inputs": {
      "name": "ImageProcessingChain",
      "description": "Test resize, flip, and blur in sequence",
      "requiresGPU": false,
      "extraTime": 0
    }
  },
  "2": {
    "class_type": "TestImageGenerator",
    "inputs": {
      "image_type": "noise",
      "width": 512,
      "height": 512,
      "seed": 42
    }
  },
  "3": {
    "class_type": "ImageResize+",
    "inputs": {
      "image": ["2", 0],
      "width": 256,
      "height": 256,
      "method": "lanczos"
    }
  },
  "4": {
    "class_type": "ImageFlip+",
    "inputs": {
      "image": ["3", 0],
      "direction": "horizontal"
    }
  },
  "5": {
    "class_type": "MaskBlur+",
    "inputs": {
      "mask": ["4", 1],
      "amount": 5
    }
  },
  "6": {
    "class_type": "AssertExecuted",
    "inputs": {
      "input": ["5", 0]
    }
  }
}
```

## What Makes a Good Test Workflow

### Do

- Use small image sizes (256x256 or 512x512) to keep execution fast.
- Use deterministic seeds so results are reproducible.
- Use `TestImageGenerator` or similar synthetic inputs instead of loading external files.
- Include an `AssertExecuted` node at the end of each processing chain.
- Test both common and edge-case configurations (e.g., zero padding, empty string input).
- Name test files descriptively: `test_image_resize.json`, not `test1.json`.

### Do Not

- Do not depend on external model files unless the models are declared in the `models:` field and the test is marked with `requiresGPU: true`.
- Do not use large images or high step counts. Tests should complete in seconds, not minutes.
- Do not hardcode absolute file paths.
- Do not test nodes that are labeled as disabled (e.g., `Incompatible`, `RequiresDisplay`).
- Do not include UI metadata, link arrays, or other non-API-format data in the JSON.

## Running Tests Locally

### Validate Test Workflow Format

This checks that all test JSON files are valid and have the required structure:

```bash
python scripts/validate_test_workflows.py
```

The validator checks:
- Valid JSON syntax
- Top-level dict structure (not array)
- Non-empty workflow
- Every node has `class_type` and `inputs`
- Test directories correspond to packs in `supported_nodes.yaml`

### Run Against a Live ComfyUI Instance

To actually execute test workflows, you need a running ComfyUI instance with the node pack installed:

```bash
# Start ComfyUI (with your custom nodes installed)
python main.py --listen 0.0.0.0 --port 8188

# In another terminal, submit a test workflow
curl -X POST http://localhost:8188/api/prompt \
  -H "Content-Type: application/json" \
  -d @tests/node-tests/comfyui-kjnodes/test_image_pad_kj.json
```

## Naming Conventions

| Convention | Example |
|------------|---------|
| Test file prefix | `test_` |
| Descriptive name | `test_image_pad_kj.json`, `test_audio_normalize.json` |
| Chain test | `test_crop_stitch_e2e.json` |
| Edge case test | `test_empty_input.json`, `test_zero_padding.json` |

## Common Pitfalls

**"My workflow works in ComfyUI but the test fails."**
Make sure you exported with "Save (API Format)", not the default "Save". The API format is a flat dict of node IDs, not the full workflow with links and UI data.

**"The validator says my directory does not match any pack."**
Check that your test directory name exactly matches the `name` in `supported_nodes.yaml`. For git-pinned URLs, use only the repository name (the part after the last `/` and before `@`).

**"My node requires a model file."**
Declare the model in the `models:` field of your `supported_nodes.yaml` entry. For testing purposes, consider whether the node can be tested without the model (e.g., testing input validation or parameter handling).
