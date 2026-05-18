---
name: custom-node-updater
description: Automated agent that checks for updates to existing custom nodes and creates PRs with version bumps. Runs on cron weekly.
model: opus
color: blue
---

# Custom Node Updater Agent

Automated weekly check for custom node updates. Runs via GitHub Actions cron (Mondays 6 AM UTC).

## Why This Exists

ComfyUI custom nodes are actively developed - authors push updates with bug fixes, new features, and compatibility improvements. Without automation:

- We'd manually check 50+ nodes for updates (hours of work)
- Updates would fall behind, missing important fixes
- Security issues in updates could slip through

This agent runs weekly to:
1. Check each node in `supported_nodes.yaml` against the registry/GitHub
2. Analyze diffs for NEW security issues (not existing ones)
3. Validate dependencies still work with cloud (numpy 2.2.6, torch 2.8.0)
4. Create **one draft PR per node** with updates (easier to review and rollback)

The goal is **visibility** - even blocked updates get documented so humans can decide.

When a version update PR is merged to this repo's main branch, the existing `notify-cloud.yml` workflow dispatches to the cloud repo, which triggers `comfy-complete-sync.yml` to update the version pin.

---

## Runtime Context

When triggered, you receive:
- **Dry run mode**: If `true`, only report updates, don't create PR
- **gh CLI**: Pre-authenticated for GitHub API calls
- **Config file**: `supported_nodes.yaml`

---

## Phase 1: Setup

```bash
REPO_ROOT=$(git rev-parse --show-toplevel)
WORK_DIR="$REPO_ROOT/.tmp/node-updates-$(date +%Y%m%d)"
mkdir -p "$WORK_DIR"/{repos,reports}
CONFIG_FILE="$REPO_ROOT/supported_nodes.yaml"
echo "[]" > "$WORK_DIR/updates.json"

```

---

## Phase 2: Check for Updates

For each node in the config, check the latest available version.

**Registry nodes** (have version like "1.2.3"):

```bash
curl -s "https://api.comfy.org/nodes/$NODE_NAME" | jq -r '.latest_version.version'

```

**GitHub commit nodes** (URL with @commit):

```bash
gh api "repos/$OWNER/$REPO/commits" --jq '.[0].sha'

```

Compare current version vs latest. Track updates in `$WORK_DIR/updates.json`.

---

## Phase 3: Analyze Each Update

For each node with an update available:

### 3.1 Clone and Diff

Clone the node repository and generate diffs between old and new versions:

```bash
NODE_DIR="$WORK_DIR/repos/$NODE_NAME"
mkdir -p "$NODE_DIR"

# For registry nodes, get the repository URL
REPO_URL=$(curl -s "https://api.comfy.org/nodes/$NODE_NAME" | jq -r '.repository')

# Clone and generate diffs
git clone "$REPO_URL" "$NODE_DIR/repo"
cd "$NODE_DIR/repo"

# Get commit SHAs for old and new versions (use tags or version lookups)
git diff "$OLD_COMMIT..$NEW_COMMIT" --stat > "$NODE_DIR/diff-stat.txt"
git diff "$OLD_COMMIT..$NEW_COMMIT" -- '*.py' > "$NODE_DIR/diff-python.txt"
git log "$OLD_COMMIT..$NEW_COMMIT" --oneline > "$NODE_DIR/commits.txt"
```

### 3.2 Security Scan on Diff

**Check for NEW security issues** (lines starting with `+` in diff):

```bash
grep -E '^\+.*eval\(' "$NODE_DIR/diff-python.txt"
grep -E '^\+.*exec\(' "$NODE_DIR/diff-python.txt"
grep -E '^\+.*os\.system' "$NODE_DIR/diff-python.txt"
grep -E '^\+.*subprocess\..*shell=True' "$NODE_DIR/diff-python.txt"
grep -E '^\+.*pickle\.(loads|dumps)' "$NODE_DIR/diff-python.txt"

```

If new security issues found -> **BLOCK** the update

### 3.3 Validate Dependencies

Check for dependency conflicts inline:

```bash
cd "$NODE_DIR/repo"

# Check for numpy<2 requirement (cloud uses 2.2.6)
if [ -f requirements.txt ]; then
    grep -iE 'numpy\s*<\s*2|numpy\s*==\s*1\.' requirements.txt && echo "BLOCKER: numpy<2 requirement"
fi

# Check for torch version conflicts (cloud uses 2.8.0)
if [ -f requirements.txt ]; then
    grep -iE 'torch\s*[<>=!]' requirements.txt && echo "WARNING: torch version constraint found"
fi

# Check Python syntax
find . -name "*.py" -exec python3 -c "import py_compile; py_compile.compile('{}', doraise=True)" \; 2>&1 | grep -i error
```

Checks for:
- `numpy<2` requirement -> **BLOCKER** (cloud uses 2.2.6)
- Torch version conflicts -> **WARNING** (cloud uses 2.8.0)
- Python syntax errors

### 3.4 Validate Existing node_labels

Check if nodes in `node_labels` still exist in new version:

```bash
# Run from the cloned node repository directory
cd "$NODE_DIR/repo"
rg 'NODE_CLASS_MAPPINGS\[' --type py | grep -oE '"[^"]+"' | tr -d '"' | sort -u
```

- If a labeled node no longer exists -> Remove from node_labels
- If new problematic nodes appear -> Add to node_labels with appropriate labels

### 3.5 Claude Diff Review (IMPORTANT)

The grep-based security scan catches obvious patterns. Now **YOU must review the actual diff** to find issues that require understanding.

**Read the diff file** using Read tool:

```bash
# Read the Python diff for this node
cat "$NODE_DIR/diff-python.txt"

```

**As you read the diff, look for NEW code (lines starting with `+`) that:**

#### A. Introduces Arbitrary File Access
- New STRING inputs for paths/files in `INPUT_TYPES`
- New file operations on user-provided paths:
  - **Reads**: `open()`, `Path().read_text()`, `shutil.copy()`
  - **Writes**: `open(..., 'w')`, `PIL.Image.save()`, `cv2.imwrite()`, `imageio.imwrite()`, `torchvision.utils.save_image()`
- New file read/write patterns that weren't there before

#### B. Adds Code Execution Vectors
- New `eval()`, `exec()` even with different variable names
- New `getattr()` with user-controlled attribute names
- New dynamic imports from user input

#### C. Adds Data Exfiltration
- New network calls with user-controlled URLs
- New webhook/callback functionality
- New external API integrations

#### D. Changes Security-Critical Logic
- Modified input validation that's now weaker
- Removed safety checks
- Changed from safe to unsafe deserialization

#### E. Cloud Compatibility Issues (NEW in this version)
- **Custom HTTP endpoints**: New `@PromptServer.instance.routes` decorators
- **Stateful nodes**: New class/module variables that store state between runs
- **Global state**: New module-level caches or singletons
- **Dependency conflicts**: New requirements that conflict with cloud versions (numpy, torch, opencv, Pillow)

**Document findings:**

```bash
# Add to the node's report.json
jq '.claude_diff_review = {
  "lines_reviewed": 150,
  "new_concerns": [
    {"severity": "HIGH", "issue": "New file path input added to SaveAnywhere node", "line": "+42"},
    {"severity": "MEDIUM", "issue": "New requests.get() with dynamic URL", "line": "+118"}
  ],
  "recommendation": "BLOCK" or "APPROVE with warning"
}' "$NODE_DIR/report.json" > "$NODE_DIR/report.tmp" && mv "$NODE_DIR/report.tmp" "$NODE_DIR/report.json"

```

**This review is critical** - the regex scan catches obvious patterns, but you catch semantic issues like "this new input flows to that dangerous operation."

---

## Phase 4: Make Decisions

For each update, decide: **APPROVE** or **BLOCK**

**APPROVE if:**
- No new security issues in diff
- No numpy<2 requirement
- Syntax checks pass

**BLOCK if:**
- New `eval()`, `exec()`, `os.system()` in diff
- New `numpy<2` requirement introduced
- Syntax errors in new code

---

## Phase 5: Update Config

For approved updates, modify `supported_nodes.yaml`:

**Registry nodes** (have `version: "X.Y.Z"`):

Use the Edit tool to update the version string for the node entry. The YAML structure is:

```yaml
node_packs:
  - name: comfyui-kjnodes
    version: "1.2.8"
```

Find the node by name and update its `version:` field.

**GitHub commit nodes** (have `name: "https://github.com/...@commit"`):

Use the Edit tool to replace the commit hash in the `name:` field URL.

---

## Phase 6: Check for Existing PRs

Before creating new PRs, check if PRs already exist for each node:

```bash
# Check for existing open PRs for this node
EXISTING_PR=$(gh pr list --state open --search "update $NODE_NAME in:title" --json number,title,url --jq '.[0]')

if [ -n "$EXISTING_PR" ]; then
    # If PR exists for same version, skip
    # If PR exists for older version, close it and create new one
fi

```

**Rules:**
- **Same version PR exists**: Skip (don't create duplicate)
- **Older version PR exists**: Close with comment "Superseded by newer version X.Y.Z", then create new PR
- **No PR exists**: Proceed to create

---

## Phase 7: Create PRs (unless dry run)

If dry run mode is enabled, **stop here** and output summary.

**IMPORTANT**: Create **one draft PR per node update** - easier to review and rollback.

For each approved update:

```bash
NODE_NAME="<node-name>"
OLD_VER="<old-version>"
NEW_VER="<new-version>"
DATE=$(date +%Y-%m-%d)

# Get repo URL for diff link
REPO_URL=$(curl -s "https://api.comfy.org/nodes/$NODE_NAME" | jq -r '.repository')

git checkout main
git checkout -b "automation/update/$NODE_NAME-$NEW_VER"
# Apply the version update to config
git add supported_nodes.yaml
git commit -m "chore(custom-nodes): update $NODE_NAME to $NEW_VER"
git push -u origin HEAD

```

Create **draft** PR with diff link and assign reviewers:

```bash
PR_URL=$(gh pr create --draft --title "chore(custom-nodes): update $NODE_NAME to $NEW_VER" --body "<template>")

# Auto-assign reviewers
PR_NUM=$(echo "$PR_URL" | grep -oE '[0-9]+$')
gh pr edit "$PR_NUM" --add-reviewer deepme987,bigcat88

```

### PR Template for Updates

```markdown
## Summary

**Node**: [<node-name>](<registry-or-github-url>)
**Update**: `<old>` → `<new>`
**Diff**: [View changes on GitHub](<repo-url>/compare/<old-commit>...<new-commit>)

---

## Analysis

| Check | Result |
|-------|--------|
| Commits | <N> |
| Security scan (diff) | ✅/❌ <details> |
| Dependencies | ✅/❌ <details> |
| node_labels | ✅ Still valid / ⚠️ Needs update |

### Notable Changes

<Summary of what changed - from diff analysis>

### Script Security Scan

Checked diff for NEW occurrences of:
- [x] No new eval()/exec() calls
- [x] No new os.system()/shell=True
- [x] No numpy<2 requirements introduced
- [x] Python syntax valid

### Claude Diff Review

Reviewed X lines of Python changes.

**Semantic Analysis**:
- [x] No new file path inputs that could allow arbitrary file access
- [x] No new indirect code execution patterns
- [x] No new data exfiltration risks
- [x] No weakened validation or removed safety checks

**Findings**:
- <List specific concerns, or "No semantic security issues found in diff">

**Recommendation**: ✅ APPROVE / ⚠️ APPROVE with warnings / 🔴 NEEDS MANUAL REVIEW

---

## Human Review Checklist

### Automated Checks
- [ ] CI passes
- [ ] Security scan passed
- [ ] Dependencies compatible

### Manual Testing
- [ ] Tested in cloud environment (if significant changes)
- [ ] Sample workflows still work

### Ready for Review
- [ ] All checks pass
- [ ] PR converted from draft to ready

---

🤖 Generated with [Claude Code](https://claude.com/claude-code)

```

### For Blocked Updates

Create a **draft** PR anyway to document the block:

```bash
gh pr create --draft --title "chore(custom-nodes): update $NODE_NAME to $NEW_VER (BLOCKED)" --body "..."

```

Include:
- Why it's blocked
- What needs to happen to unblock
- Link to diff so humans can review

---

## Version Constraints

**Cloud versions are LOCKED - cannot be changed:**
- `numpy==2.2.6` - Nodes requiring `numpy<2` are **INCOMPATIBLE**
- `torch==2.8.0` - From `pytorch/pytorch:2.8.0-cuda12.8-cudnn9-devel`

---

## Decision Guidelines

1. **What counts as a security issue?**
   - New code that wasn't in previous version
   - Only `+` lines in diff matter, not existing code

2. **Should we update if CI might fail?**
   - Yes, create the PR anyway
   - CI failure flags it for human review
   - Better to have visibility than silent skip

3. **What if no updates found?**
   - Output "All nodes are up to date"
   - Don't create empty PR

4. **What if ALL updates are blocked?**
   - Still create PR documenting the blocks
   - Humans can decide to override

---

## Output

At completion, report:
- Total nodes checked
- Updates found
- Updates approved
- Updates blocked
- PR URL (if created)
