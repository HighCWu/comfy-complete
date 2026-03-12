# ComfyUI Environment Detection API

This document describes the environment detection API that lets custom nodes adapt their behavior based on the execution environment.

The design decision is to use **environment detection** (not feature flags). Labels describe what a node does (static metadata for deployment policy); environment detection lets nodes adapt behavior at runtime.

## API

```python
import comfy.utils

env = comfy.utils.get_execution_environment()  # Returns: "local" | "cloud" | "remote"
```

- **Default:** `"local"` (when no environment variable is set)
- **Cloud sidecar** sets `COMFY_EXECUTION_ENVIRONMENT=cloud`
- **Other managed deployments** can set their own value (e.g., `"remote"`)

The function reads `os.environ.get("COMFY_EXECUTION_ENVIRONMENT", "local")` and returns the result as a lowercase string.

## How Node Authors Should Use It

### Pattern 1: Graceful degradation

Use this when your node can function in both local and managed environments but with different code paths.

```python
def execute(self, ...):
    env = comfy.utils.get_execution_environment()
    if env == "local":
        # Full functionality
        result = self.download_and_process(url)
    else:
        # Managed deployment — models are pre-provisioned
        result = self.load_from_cache()
    return result
```

### Pattern 2: Skip unsupported features

Use this when a feature fundamentally requires local access (filesystem, hardware, etc.).

```python
def execute(self, ...):
    env = comfy.utils.get_execution_environment()
    if env != "local":
        raise RuntimeError("This node requires local filesystem access")
```

### Pattern 3: Conditional network access

Use this when a node downloads models but can fall back to pre-provisioned assets.

```python
def execute(self, model_name, ...):
    env = comfy.utils.get_execution_environment()
    model_path = folder_paths.get_full_path("checkpoints", model_name)

    if model_path and os.path.exists(model_path):
        # Model already available (pre-provisioned or cached)
        return self.load_model(model_path)
    elif env == "local":
        # Local environment — download on demand
        return self.download_and_load(model_name)
    else:
        raise RuntimeError(f"Model {model_name} not pre-provisioned in managed environment")
```

## Relationship to Labels

Labels and environment detection serve different purposes:

| Aspect | Labels | Environment Detection |
|--------|--------|-----------------------|
| **Type** | Static metadata | Runtime API |
| **Who uses it** | Deployment operators | Node authors |
| **Purpose** | Deployment policy decisions | Graceful runtime adaptation |
| **When evaluated** | Build/deploy time | Execution time |

- **Labels** describe what a node DOES (static metadata)
- **Environment detection** lets nodes ADAPT behavior at runtime
- Labels are for deployment policy; env detection is for graceful degradation
- A node with `NetworkAccess` label might check the environment and skip downloads when pre-provisioned

## When to Use Environment Detection vs Labels

| Scenario | Use Labels | Use Env Detection |
|----------|-----------|-------------------|
| Node reads arbitrary file paths | `ReadsArbitraryFile` | No -- label is sufficient |
| Node downloads models but has cache fallback | No label needed | Yes -- skip download in managed env |
| Node shows webcam preview | `RequiresWebcam` | Optionally -- show placeholder |
| Node uses `eval()` on user input | `ExecutesArbitraryCode` | No -- label blocks it |
| Node writes temp files but can use memory instead | No label needed | Yes -- switch to in-memory in managed env |
| Node installs pip packages at runtime | `RuntimePipInstall` | No -- label blocks it |
| Node registers custom HTTP endpoints | `HasCustomEndpoints` | Optionally -- skip registration in managed env |

## Guidelines for Node Authors

1. **Always provide a fallback.** If your node can work without a feature, use environment detection to degrade gracefully rather than crashing.

2. **Do not use environment detection to bypass security.** If a label blocks your node in a deployment, environment detection should not be used to circumvent the restriction.

3. **Check for the environment once, early.** Avoid calling `get_execution_environment()` deep in hot loops. Read it once and store the result.

4. **Document your behavior differences.** If your node behaves differently in managed environments, document what changes in your node's README or docstring.

5. **Test both paths.** Set `COMFY_EXECUTION_ENVIRONMENT=cloud` locally to verify your managed-environment code path works correctly.
