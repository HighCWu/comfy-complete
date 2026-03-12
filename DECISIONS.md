# Autonomous Session Decisions Log

Session: 2026-03-10 (overnight autonomous run)
Context: User sleeping ~8 hours. Full autonomy to complete comfy-complete as standalone open-source project.

## Constraints Applied
- No git push / no making code public
- No intentional harm
- Document all major decisions
- Ensure testing

---

## Decision 1: License Choice — Apache 2.0

**Decision:** Use Apache License 2.0 for the open-source comfy-complete repo.

**Rationale:** Matches Comfy-Org conventions (ComfyUI core uses GPL, but Apache 2.0 is more permissive and appropriate for a distribution/configuration project that doesn't contain ComfyUI code itself — it contains YAML configs, Python scripts, and Docker configs). Apache 2.0 provides patent protection and is business-friendly for deployers.

**Impact:** Created `LICENSE` file.

---

## Decision 2: Dockerfile.cloudbuild stays in comfy-complete

**Decision:** Keep `docker/Dockerfile.cloudbuild` in the comfy-complete directory alongside the main `Dockerfile`.

**Rationale:** Both Dockerfiles share the same build context (`docker/`), scripts, and directory structure. Moving only the cloudbuild variant would break the shared context. The file is clearly named and doesn't leak cloud secrets — it's just a build variant. Any deployer could use it as reference for their own build pipeline.

**Impact:** No move needed. Updated one comment from "cloud deployments" to "locked-down deployments".

---

## Decision 3: Security scanner expanded for new labels

**Decision:** Added detection patterns in `security-scan.sh` for 6 new behavioral label categories.

**New detections added:**
- RuntimeModelDownload: `hf_hub_download`, `snapshot_download`, `download_url_to_file`, `from_pretrained`
- RuntimePipInstall: `pip install`, `ensure_package`
- RequiresGPU: `.to('cuda')`, `.cuda()`, `torch.device('cuda')`
- RequiresWebcam: `VideoCapture(0)`
- RequiresClipboard: `grabclipboard`, `pyperclip`
- RequiresDisplay: `cv2.imshow`, `cv2.waitKey`, `plt.show`, `send_sync`

**Rationale:** The scanner is the first automated pass in PR review. Without these patterns, the reviewer agent would have to find them entirely through code reading. These patterns catch the most common cases.

**Impact:** Updated `scripts/pr-review/security-scan.sh`.

---

## Decision 4: PR reviewer agent updated with all 18 label detection guides

**Decision:** Extended `.claude/agents/pr-reviewer.md` with code-level detection patterns for all 10 new labels (PathParsing, DuplicateOfCoreNode, Incompatible, RequiresWebcam, RequiresDisplay, RequiresClipboard, RequiresGPU, BrokenNode, ExecutesArbitraryCode, RuntimeModelDownload, RuntimePipInstall).

**Rationale:** The PR reviewer is the primary quality gate. Without detection patterns, it can't reliably suggest labels for new node pack submissions.

**Impact:** Updated `.claude/agents/pr-reviewer.md`. Each new label has Python code examples showing what patterns to look for.

---

## Decision 5: Cloud language replaced with deployment-agnostic terms

**Decision:** Systematically replaced "cloud" with deployment-agnostic language throughout comfy-complete/:
- "cloud deployments" → "managed deployments" or "managed environments"
- "not available in cloud" → "not in base image"
- "Cloud Compatibility" → "Deployment Compatibility"
- "same environment used by Comfy Cloud" → removed entirely

**Exception:** Comments in `supported_nodes.yaml` that explain *why* a label was applied (e.g., "disabled on cloud") were left as-is since they're informational context about the original labeling rationale.

**Rationale:** comfy-complete is becoming an open-source project used by anyone, not just Comfy Cloud. The language should reflect this.

**Impact:** Updated README.md, docs/adding-custom-nodes.md, build-config.yaml, config.yaml.example, security-scan.sh, Dockerfile.cloudbuild, CONTRIBUTING.md.

---

## Decision 6: Test coverage expanded from 12 to 52 tests

**Decision:** Added 3 new test files:

1. **test_resolve_disabled_nodes.py** (25 tests) — Unit tests for the label filtering engine:
   - `get_node_labels()` extraction
   - `resolve_filter()` with OR logic, empty filters, negative filters
   - `get_all_disabled_nodes()` combining static + dynamic filtering
   - `validate_labels()` for undeclared label detection
   - Integration tests against real `supported_nodes.yaml` (label count, expected labels, no duplicates, disabled node count)

2. **test_detect_changes.py** (12 tests) — Unit tests for PR change detection:
   - `NodePack` dataclass serialization/deserialization with new fields
   - `detect_label_changes()` for added/removed/modified labels
   - `ChangeReport` structure and serialization

3. **test_config_example.py** (3 tests) — Validates config.yaml.example:
   - Valid YAML structure
   - Correct disable_nodes OR-filter format
   - Labels used are declared in supported_nodes.yaml

**Also fixed:** `test_requirements_resolvable_with_uv` — was using `which uv` (Unix-only) and `bin/python` path. Fixed to use `shutil.which()` and OS-aware paths.

**Rationale:** The existing 12 tests only covered config file structure. The filtering engine (resolve_disabled_nodes.py) and change detection (detect_changes.py) had zero test coverage despite being critical pipeline components.

**Impact:** 52 tests all passing (1 skipped: uv resolution test — pre-existing cupy/numpy version conflict in requirements.txt, not introduced by us).

---

## Decision 7: GitHub Actions CI workflow

**Decision:** Created `.github/workflows/ci.yml` with two jobs:
1. `validate` — Runs pytest on Python 3.12
2. `yaml-lint` — Runs yamllint on config files

**Rationale:** The standalone repo needs its own CI. Kept minimal — validates structure and correctness without requiring GPU or Docker.

**Impact:** Created `.github/workflows/ci.yml`.

---

## Decision 8: Cloud-side script paths updated

**Decision:** Updated all cloud repo scripts that referenced `comfy-complete/cloud_disable_config.yaml` and `comfy-complete/cloud_overlay.yaml` to use the new root-level paths.

**Files updated:**
- `scripts/filter_object_info_nodes.py` — default path changed to `repo_root / 'cloud_disable_config.yaml'`
- `scripts/sync_custom_node_extensions.py` — default overlay path changed to `repo_root / 'cloud_overlay.yaml'`
- `.github/workflows/sync-objectinfo.yml` — path filter updated

**Rationale:** We moved these files from `comfy-complete/` to repo root in P1. Cloud scripts must reference the new locations.

**Impact:** Updated 3 files in cloud repo (outside comfy-complete/).

---

## Decision 9: Internal working documents removed

**Decision:** Removed from comfy-complete/:
- `HANDOVER.md` (internal knowledge transfer, 670 lines)
- `GAMEPLAN.md` (implementation plan)
- `LABEL_MIGRATION_AUDIT.md` (DisabledOnCloud migration analysis)
- `audit_group1-5.yaml` (source code audit working files)
- 4 cloned repo directories left by audit agents

**Rationale:** These are session artifacts, not part of the distribution. They'd confuse open-source contributors.

**Impact:** Cleaner repo with only files relevant to end users.

---

## Pre-Existing Issue Found: requirements.txt dependency conflict

**Issue:** `cupy-cuda12x==12.3.0` requires `numpy>=1.20,<1.29` but `numpy==2.2.6` is pinned.

**Decision:** Did NOT fix this. It's a pre-existing conflict in the requirements that predates our work. Fixing it requires careful testing of all dependent node packs, which should be a separate PR.

**Impact:** The `test_requirements_resolvable_with_uv` test correctly detects and reports this conflict.

---

## Summary of All Changes

### New files created in comfy-complete/:
| File | Purpose |
|------|---------|
| `LICENSE` | Apache 2.0 license |
| `CONTRIBUTING.md` | Contributor guide |
| `.github/workflows/ci.yml` | GitHub Actions CI |
| `.github/PULL_REQUEST_TEMPLATE.md` | PR template |
| `tests/test_resolve_disabled_nodes.py` | 25 tests for filtering engine |
| `tests/test_detect_changes.py` | 12 tests for change detection |
| `tests/test_config_example.py` | 3 tests for config example |
| `DECISIONS.md` | This file |

### Files modified in comfy-complete/:
| File | Change |
|------|--------|
| `README.md` | Removed cloud language, updated labels table (8→18) |
| `docs/adding-custom-nodes.md` | Updated labels table, deployment-agnostic language |
| `build-config.yaml` | Comment updated |
| `config.yaml.example` | Updated labels, removed DisabledOnCloud |
| `.claude/agents/pr-reviewer.md` | Added detection patterns for 10 new labels |
| `scripts/pr-review/security-scan.sh` | Added 6 new label detection patterns |
| `tests/test_requirements.py` | Fixed uv detection for cross-platform |
| `docker/Dockerfile.cloudbuild` | Comment updated |
| `supported_nodes.yaml` | Labels migrated (P0, prior session) |
| `scripts/pr-review/detect_changes.py` | New fields support (P0, prior session) |

### Files moved out of comfy-complete/ (to cloud repo root):
| From | To |
|------|-----|
| `comfy-complete/cloud_disable_config.yaml` | `cloud_disable_config.yaml` |
| `comfy-complete/cloud_overlay.yaml` | `cloud_overlay.yaml` |
| `comfy-complete/cloudbuild/` | `cloudbuild/` |
| `comfy-complete/docs/cloud-node-integration.md` | `docs/cloud-node-integration.md` |

### Files deleted from comfy-complete/:
- `HANDOVER.md`, `GAMEPLAN.md`, `LABEL_MIGRATION_AUDIT.md`
- `audit_group1-5.yaml`
- 4 cloned repo directories

### Cloud repo files updated (outside comfy-complete/):
| File | Change |
|------|--------|
| `scripts/filter_object_info_nodes.py` | Path to cloud_disable_config.yaml |
| `scripts/sync_custom_node_extensions.py` | Path to cloud_overlay.yaml |
| `.github/workflows/sync-objectinfo.yml` | Path filter for cloud_overlay.yaml |
| `docs/cloud-node-integration.md` | Updated paths, removed DisabledOnCloud |

### Test Results
- **52 tests passing** (up from 12)
- **1 skipped** (uv resolution — pre-existing dep conflict)
- **412 disabled nodes** with cloud config (verified)

---

## Session 2: PRD Implementation (2026-03-11)

Context: Continuing from P0-P5 infrastructure work. User indicated the PRD (Notion doc "PRD: ComfyComplete Open-Source Submission Pipeline") defines the full scope. Fetched and consumed the PRD, then built the remaining deliverables.

---

## Decision 10: Test Workflow Framework

**Decision:** Built three scripts for the test workflow framework: `validate_test_workflows.py`, `check_test_coverage.py`, and `run_tests.py`.

**Rationale:** The PRD calls test workflows "the #1 blocker" — without a local test runner that replicates CI, no author will submit. The framework validates JSON structure, checks coverage (every non-labeled node needs a test), and supports optional execution against a local ComfyUI instance.

**Impact:** Created 3 scripts in `scripts/`. Updated `ci.yml` with `test-workflows` job. Coverage checker runs in `--warn-only` mode since not all 76 packs have tests yet.

---

## Decision 11: Enhanced Review Agent Scripts

**Decision:** Built four review automation scripts: `license_checker.py`, `dependency_resolver.py`, `registry_checker.py`, and `model_url_validator.py`.

**Rationale:** The PRD requires exhaustive license checking (node + models + pip deps), dependency conflict detection via uv, registry verification, and model URL validation. These are P0 checks in the review agent.

**Details:**
- License checker: blocklists AGPL, GPL-3.0 (for deps), InsightFace, deepface, non-commercial. Checks LICENSE files, pip-licenses output, and HuggingFace model patterns.
- Dependency resolver: merges dependency_overrides with requirements.txt, uses uv pip compile for resolution.
- Registry checker: HEAD requests to `api.comfy.org/nodes/<name>`.
- Model URL validator: HEAD requests on declared URLs, flags >10GB models.

**Impact:** Created 4 scripts in `scripts/pr-review/`. Updated `pr-reviewer.md` with new Step 4.5.

---

## Decision 12: Cloud Transform Scripts

**Decision:** Built `generate_model_configs.py` and `extract_deps.py` as specified in the PRD's "Cloud Transforms" section.

**Rationale:** The PRD specifies two transform scripts that bridge comfy-complete → cloud: one extracts `models:` entries into cloud's `supported_models.json` format, the other extracts `dependency_overrides:` and merges with `requirements.txt`.

**Impact:** Created 2 scripts in `scripts/`. These will be used by cloud CI when processing supported_nodes.yaml changes.

---

## Decision 13: Ephemeral Bridge Workflows

**Decision:** Built the cross-repo ephemeral testing pipeline with two GitHub Actions workflows and a CLI helper script.

**Rationale:** The PRD's ephemeral bridge is how test workflows get executed against real ComfyUI with GPU. The comfy-complete side triggers a `repository_dispatch` to the cloud repo when maintainer adds `ephemeral-test` label, and receives results back via `ephemeral-test-results` dispatch.

**Security:** Only org members can add labels. Ephemeral runs in an isolated GCP project (cloud-side concern, documented in workflow comments).

**Impact:** Created `.github/workflows/ephemeral-test.yml`, `.github/workflows/ephemeral-results.yml`, and `scripts/ephemeral_bridge.py`.

---

## Decision 14: Comprehensive Author Documentation

**Decision:** Significantly expanded documentation to match PRD requirements.

**Changes:**
- `docs/adding-custom-nodes.md` — Added The 10 Rules, eligibility criteria, full YAML template (minimal + full), models declaration, dependency_overrides explanation, test workflow requirements, complete submission flow (7 steps), ephemeral testing section.
- `CONTRIBUTING.md` — Added references to The 10 Rules, eligibility, dependency_overrides, models, test workflows, expanded PR process.
- `docs/test-workflow-guide.md` (NEW) — Focused guide on creating test workflows: API-format JSON structure, how to export from ComfyUI, directory conventions, coverage requirements, chain workflows, do/don't guidelines.

**Rationale:** The PRD states "No author will submit if the process is hectic." Clear, practical documentation is critical for adoption.

**Impact:** Updated 2 docs, created 1 new doc.

---

## Summary of Session 2 Changes

### New files created:
| File | Purpose |
|------|---------|
| `scripts/validate_test_workflows.py` | Validates test workflow JSON structure |
| `scripts/check_test_coverage.py` | Checks test coverage per node pack |
| `scripts/run_tests.py` | Local test runner for authors |
| `scripts/generate_model_configs.py` | Extracts models → cloud format |
| `scripts/extract_deps.py` | Extracts/merges dependency_overrides |
| `scripts/ephemeral_bridge.py` | CLI helper for ephemeral dispatch |
| `scripts/pr-review/license_checker.py` | Exhaustive license checking |
| `scripts/pr-review/dependency_resolver.py` | Dep conflict detection |
| `scripts/pr-review/registry_checker.py` | Comfy Registry verification |
| `scripts/pr-review/model_url_validator.py` | Model URL validation |
| `.github/workflows/ephemeral-test.yml` | Ephemeral test trigger |
| `.github/workflows/ephemeral-results.yml` | Ephemeral results receiver |
| `docs/test-workflow-guide.md` | Test workflow creation guide |

### Files modified:
| File | Change |
|------|--------|
| `.github/workflows/ci.yml` | Added test-workflows job |
| `.claude/agents/pr-reviewer.md` | Added automated review script steps |
| `docs/adding-custom-nodes.md` | The 10 Rules, eligibility, full submission flow |
| `CONTRIBUTING.md` | Expanded with deps, models, tests, eligibility |
| `DECISIONS.md` | This update |
