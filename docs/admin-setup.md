# Admin Setup Guide

Manual steps required to complete the comfy-complete infrastructure. These require org admin or repo admin permissions that can't be done via CI.

## 1. Repository Secrets (Comfy-Org/comfy-complete)

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

## 2. Branch Protection (Comfy-Org/comfy-complete)

GitHub → Comfy-Org/comfy-complete → Settings → Branches → Add rule for `main`:

- [x] **Require a pull request before merging**
  - [x] Require approvals: **1**
  - [x] Dismiss stale pull request approvals when new commits are pushed
- [x] **Require status checks to pass before merging**
  - Required checks:
    - `validate` (from CI workflow)
    - `yaml-lint` (from CI workflow)
    - `test-workflows` (from CI workflow)
- [x] **Restrict who can push to matching branches**
  - Only org members / maintainers
- [x] **Do not allow force pushes**
- [x] **Do not allow deletions**

---

## 3. CODEOWNERS (Comfy-Org/comfy-complete)

Create `.github/CODEOWNERS` in the comfy-complete repo:

```
# Backend team must approve all node configuration changes
supported_nodes.yaml    @Comfy-Org/backend
requirements.txt        @Comfy-Org/backend
version_lock.yaml       @Comfy-Org/backend
build-config.yaml       @Comfy-Org/backend
```

This ensures node config changes require backend team approval even if the repo is public.

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

| Step | Urgency | Blocks |
|------|---------|--------|
| 1. ANTHROPIC_API_KEY | Now | Claude review agent on PRs |
| 2. Branch protection | Now | Prevents unreviewed merges |
| 3. CODEOWNERS | Now | Requires backend team approval |
| 4. CLOUD_REPO_PAT | Before ephemeral testing | Ephemeral test dispatch |
| 5. GitHub App access | After merge | Simplifies Cloud Build auth |
| 6. ArgoCD cleanup | After merge | Removes workarounds |
| 7. Secret Manager cleanup | After step 5 | Removes unused secret |
| 8. Docker Hub | When frontend needs it | Frontend testing |
