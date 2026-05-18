# Admin Setup Guide

Configuration for the comfy-complete repository. Tracks what has been applied
and what still requires manual action.

## Repo Settings (APPLIED 2026-03-27)

| Setting | Value | Matches |
|---------|-------|---------|
| Merge method | Squash only | ComfyUI_frontend |
| Delete branch on merge | Yes | ComfyUI_frontend |
| Wiki | Disabled | — |
| Auto-merge | Disabled | ComfyUI core |

## Branch Ruleset: `ProtectMain` (APPLIED 2026-03-27)

Ruleset ID `14420755` — uses GitHub's rulesets API (same as ComfyUI core and
ComfyUI_frontend, NOT legacy branch protection).

| Rule | Configuration |
|------|--------------|
| No deletion | Prevent main branch deletion |
| No force push | Prevent non-fast-forward pushes |
| Linear history | Required |
| Pull request | 1 approval, code owner review, stale review dismissal, thread resolution required, squash only |
| Required checks | `validate`, `yaml-lint`, `test-workflows` |
| Bypass actors | None — nobody can bypass |

The `ai-review` check is intentionally NOT required — it only runs when
`ANTHROPIC_API_KEY` is configured and skips bot actors.

View/edit: https://github.com/Comfy-Org/comfy-complete/rules/14420755

## CODEOWNERS (APPLIED 2026-03-27)

File: `/CODEOWNERS` (repo root, matching ComfyUI core's placement)

| Pattern | Owners | Purpose |
|---------|--------|---------|
| `*` (default) | `@Comfy-Org/comfy-cloud-team` | All files require cloud team review |
| `/supported_nodes.yaml` | `@deepme987 @bigcat88` | Node definitions — critical path for submissions |
| `/.github/` | `@deepme987` | CI and automation |
| `/scripts/` | `@deepme987` | Review and build scripts |
| `.claude/`, `.cursor/`, `**/CLAUDE.md` | (none) | LLM config — no owner review needed |

## Team Access (APPLIED 2026-03-27)

| Team | Permission | Purpose |
|------|-----------|---------|
| `comfy-cloud-team` | Write (`push`) | Code ownership, PR reviews |
| `comfy-bots` | Read (`pull`) | Automation (CI bots) |

---

## 1. Repository Secrets — MANUAL ACTION REQUIRED

### ANTHROPIC_API_KEY
Enables the Claude AI review agent on PRs. Same key used on `archived-comfy-complete`.

```bash
gh secret set ANTHROPIC_API_KEY --repo Comfy-Org/comfy-complete --body "sk-ant-..."
```

Or copy from: GitHub → Comfy-Org/archived-comfy-complete → Settings → Secrets → ANTHROPIC_API_KEY

### CLOUD_REPO_PAT
Allows comfy-complete to dispatch ephemeral test events to the cloud repo. Needs `repo` scope on `Comfy-Org/cloud`.

```bash
gh secret set CLOUD_REPO_PAT --repo Comfy-Org/comfy-complete --body "ghp_..."
```

Can reuse the same classic PAT used for `SUBMODULE_PAT` on the cloud repo.

### SUBMODULE_PAT
Used by CI to clone comfy-complete (while private). Same token as cloud repo's `SUBMODULE_PAT`.

```bash
gh secret set SUBMODULE_PAT --repo Comfy-Org/comfy-complete --body "ghp_..."
```

Not needed once comfy-complete goes public.

---

## 4. GitHub App Access (Cloud Build)

The Cloud Build GitHub App on `Comfy-Org` needs access to `comfy-complete` for the clone-at-build-time pattern. Once added, the `github-submodule-token` Secret Manager secret is no longer needed.

GitHub → Comfy-Org → Settings → Installed GitHub Apps → Google Cloud Build → Configure → Repository access → Add `comfy-complete`

After adding: remove the `--secret id=github_token` from inference cloudbuild files and the `availableSecrets` blocks. The Docker clone step will work with the default Cloud Build credentials.

Not urgent — the Secret Manager token works as a bridge until this is done.

---

## 5. ArgoCD Cleanup (post-merge of PR #2909)

After PR #2909 merges and everything works on main:

```bash
# Remove the env var we set to disable submodule recursion
kubectl set env deployment/gitops-argocd-repo-server -n argocd ARGOCD_GIT_MODULES_ENABLED-

# Remove the repo secret we created for comfy-complete (no longer needed)
kubectl delete secret repo-comfy-complete -n argocd
```

Also revert the `reposerver.enable.git.submodule` configmap setting:
```bash
kubectl patch configmap argocd-cmd-params-cm -n argocd --type=json \
  -p='[{"op":"remove","path":"/data/reposerver.enable.git.submodule"}]'
```

---

## 6. Secret Manager Cleanup (post-merge)

The `github-submodule-token` in GCP Secret Manager can be removed once Cloud Build GitHub App has direct access to comfy-complete (step 4).

```bash
gcloud secrets delete github-submodule-token --project=comfy-cloud-dev
```

---

## 7. Docker Hub Access (optional, for frontend team)

If the frontend team needs the comfy-complete Docker image on Docker Hub:

```bash
gh secret set DOCKER_USERNAME --repo Comfy-Org/comfy-complete --body "comfyorg"
gh secret set DOCKER_PASSWORD --repo Comfy-Org/comfy-complete --body "..."
```

Copy from: archived-comfy-complete already has `DOCKER_USERNAME` and `DOCKER_PASSWORD`.

Then add a push step to `cloudbuild/cloudbuild.yaml` to re-tag and push to Docker Hub after Artifact Registry.

---

## Priority Order

| Step | Urgency | Status |
|------|---------|--------|
| Repo settings (squash, delete branch) | Now | **DONE** |
| Branch ruleset (ProtectMain) | Now | **DONE** |
| CODEOWNERS | Now | **DONE** (needs commit+push) |
| Team access (comfy-cloud-team) | Now | **DONE** |
| ANTHROPIC_API_KEY secret | Now | **TODO** — enables AI review agent |
| CLOUD_REPO_PAT secret | Before ephemeral testing | **TODO** — enables cross-repo dispatch |
| GitHub App access | After cloud PR #2909 merge | TODO — simplifies Cloud Build auth |
| ArgoCD cleanup | After cloud PR #2909 merge | TODO — removes submodule workarounds |
| Secret Manager cleanup | After GitHub App access | TODO — removes unused secret |
| Docker Hub | When frontend needs it | TODO — frontend testing |

## Verification Checklist

After committing CODEOWNERS and setting up secrets, verify by opening a test PR:

- [ ] PR requires at least 1 approval from a code owner
- [ ] PR cannot merge without `validate`, `yaml-lint`, `test-workflows` passing
- [ ] Stale reviews are dismissed when new commits are pushed
- [ ] Only squash merge is available
- [ ] Branch is auto-deleted after merge
- [ ] `custom-node-review.yml` runs AI review (if `ANTHROPIC_API_KEY` configured)
- [ ] Direct pushes to main are blocked
