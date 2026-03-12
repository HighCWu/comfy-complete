# ComfyComplete

A curated distribution of ComfyUI — vetted, version-pinned, tested bundle of custom nodes.

## Commit Rules

- **NEVER** add `Co-Authored-By` trailers to commits. ComfyUI's CI rejects AI co-author trailers.
- Commit messages: conventional commits format (`feat:`, `fix:`, `test:`, `docs:`, `chore:`)
- Keep commits clean — squash fixups, no WIP commits in PRs

## Repository Structure

- `supported_nodes.yaml` — Single source of truth for all node packs, labels, and dependencies
- `requirements.txt` — Pinned pip dependencies for the entire distribution
- `version_lock.yaml` — Pinned ComfyUI core + frontend versions
- `config.yaml.example` — Example deployment policy (which labels to disable)
- `docker/` — Dockerfiles for building the distribution image
- `scripts/add-node/` — Contributor tooling (security scan, label suggestions, license check, dep check)
- `scripts/pr-review/` — CI review scripts (change detection, security scan, node cloning)
- `tests/` — pytest unit tests for config validation
- `tests/node-tests/` — Test workflow JSON files per node pack
- `docs/` — Contributor guides

## Key Concepts

### Labels
18 behavioral labels in `supported_nodes.yaml` describe what nodes do. Each deployment decides which labels to restrict via its own policy file (e.g., `config.yaml`). Labels are metadata, not policy.

### Single-File Submission
Contributors add/update entries in `supported_nodes.yaml` only. CI validates everything.

### Scripts (not agents)
All validation is done via transparent, auditable scripts in `scripts/add-node/`:
- `check-node.sh` — All-in-one orchestrator
- `suggest-labels.py` — Pattern-matches code to suggest labels
- `check-license.py` — Exhaustive license checking (node + deps + models)
- `compile-deps.py` — Dependency conflict detection with blacklist/overrides
- `validate-entry.py` — YAML entry structure validation

### Test Workflows
- `tests/node-tests/<pack>/` — API-format JSON workflows
- Validated by `scripts/validate_test_workflows.py`
- Coverage checked by `scripts/check_test_coverage.py`
- Executed by `scripts/run-local-tests.py` against running ComfyUI

## Testing

```bash
# Run unit tests
python -m pytest tests/ -v

# Validate test workflow structure
python scripts/validate_test_workflows.py

# Check test coverage
python scripts/check_test_coverage.py --warn-only
```

## Adding a Node Pack

```bash
# Run all checks on a node repo
./scripts/add-node/check-node.sh https://github.com/author/ComfyUI-MyNode --name comfyui-mynode
```

See `docs/adding-custom-nodes.md` for the full guide.
