---
status: approved
issue: 1973
intent: intent/2026-11-16-1973-agent-token-admin-guard.md
---

# Spec: refuse an admin-scoped GitHub token for agent children

## Design

### 1. Scope guard in `runner_github_env` (`apps/web-server/server/utils/subprocess_env.py`)

`runner_github_env()` is the one place that decides which GitHub token a runner subprocess, PR review or build Job gets (`build_backend.py:401`, `make_subprocess_env(github=True)`). Both non-App branches (the agent token, and the fallback to the control plane's token) pass through a new check:

- `admin_scopes(token) -> frozenset[str] | None`. Tokens starting `ghs_` (installation), `github_pat_` (fine-grained) or `ghu_` (GitHub App user token) return `None`: they carry App permissions, not OAuth scopes, so there is no scope header to read; they are not checked, and a fine-grained token with Administration permission passes (known limit). Any other token (classic `ghp_`, OAuth `gho_`, legacy): one `GET <GITHUB_API_URL or https://api.github.com>/user` with a 5 s timeout, reading `X-OAuth-Scopes` (an absent header is `UNKNOWN`, an explicitly empty one is no scopes); the result is the intersection with `ADMIN_SCOPES = {delete_repo, admin:org, admin:org_hook, admin:repo_hook, admin:public_key, admin:gpg_key, admin:ssh_signing_key, admin:enterprise, site_admin}`. A transport error or non-200 returns the sentinel `UNKNOWN`. Results are cached per `sha256(token)` for one hour, so the call is rare.
- Policy variable `AIFACTORY_AGENT_TOKEN_ADMIN_POLICY`: `warn` (default) or `refuse`; any other value is treated as `refuse` (fail closed on a typo).
  - finding and `warn`: log one WARNING per token hash with scope names only; the token still passes.
  - finding or `UNKNOWN` and `refuse`: log one ERROR and return `{}` (no GitHub variable reaches the child). Children then fail their push with the existing warning, which is the intended fail-closed outcome for repo-admin operations.
- With the App, multi-installation mode never reaches this path and single-installation tokens are `ghs_`. `AIFACTORY_AGENT_GITHUB_TOKEN` takes precedence over the published App token in `runner_github_env`, so the cutover must also leave that variable unset if children are meant to use the App token; the runbook says so.
- **Where the refusal is enforced.** `dotenv_key_blocked(key)` also returns true for every GitHub token name whenever the policy is `refuse`, so the backend `.env` loaders (`agent_service._load_backend_env`, spec creation, PR review) cannot bring the refused token back. Tenant-saved per-project `GITHUB_TOKEN` (`project_env.PROJECT_ENV_KEYS`, loaded by `project_settings_store.load_project_env`) is a credential the project owner supplied, not the control plane's; it is out of scope and unchanged.

**Scope boundary (honest).** The guard covers tokens handed to agent children via `runner_github_env`. The control plane's own git/gh calls (`project_github_env`/`github_env`: clone, PR endgame, `gh.py`, `task_branch.py`, copilot dispatch, workspace) legitimately run with the control plane's credential and are not changed; after the App cutover that credential is the narrow installation token. Making those paths narrow is the App cutover, not this guard.

The classic `repo` scope still implies repository admin on repos the user administers. The guard cannot remove that; the real fix is the App, which is why the guard is the safety net and the App is the cutover.

### 2. Manifest and docs

`scripts/github-app-manifest.json` `default_permissions` becomes `contents: write, pull_requests: write, issues: write, checks: read, metadata: read` (owner decision 2026-10-31; no Actions, Workflows, Administration, Secrets, Organization or Members). `--workflows` still adds `workflows: write`. `tests/test_github_app_setup.py::test_manifest_permissions` is updated. `docs/docs/concepts/github-app.md`: the manual permission list matches, the policy variable and the owner cutover order are documented, and the doc points at the gitops runbook.

### 3. Gitops wiring (factory-gitops, separate PR)

`apps/aifactory/manifests/manifests.yaml`: three `secretKeyRef` env entries (`AIFACTORY_GITHUB_APP_ID`, `AIFACTORY_GITHUB_APP_INSTALLATION_ID`, `AIFACTORY_GITHUB_APP_PRIVATE_KEY`, from `factory-secrets`) with `optional: true`, so absent keys leave the variables unset and `start()` is a no-op; plus `AIFACTORY_AGENT_TOKEN_ADMIN_POLICY: "warn"`. The three App keys are all-or-none: `start()` raises on a partial set, so the owner adds all three together. The `GITHUB_TOKEN` line stays until cutover because `start()` refuses to boot with any PAT present (`GITHUB_TOKEN`, `GH_TOKEN`, `GITHUB_PERSONAL_ACCESS_TOKEN`, the MCP `tokenEnv` alias, or a token in `gh` `hosts.yml`); the gitops Deployment sets only `GITHUB_TOKEN`, but the runbook has the owner confirm the other four are absent from the pod. `docs/aifactory-github-app-cutover.md` gives the exact order: create the App from the manifest (owner), install it on the Factory repos only, add the three keys to `factory-secrets`, **in the same window** merge the one-line removal of the `GITHUB_TOKEN` env entry (a pod restart between the two steps would crash-loop), verify boot, run `probe-copilot` from a workstation (#1701), revoke the admin PAT scopes, then set the policy to `refuse`.

## Alternatives rejected

- **`refuse` as the default**: the live PAT is admin-scoped; every agent push would stop on deploy.
- **Always drop the control plane token from children (no fallback)**: same breakage, and the App path already does the right thing once configured.
- **Probing the GitHub API for fine-grained token permissions**: no stable endpoint; not worth it for the transitional guard.
- **Closing #1973/#1700/#1701 now**: each ends on an owner step (the App, the probe, the second installation).
- **Guarding the control plane's own git/gh calls**: they need a credential to clone and merge; they change by the App cutover instead.
- **Per-tenant App keys (#1700)**: already decided against (2026-10-31): one deployment-wide key plus per-org installation ids.

## Risks

- A synchronous HTTP call in a spawn path: bounded by a 5 s timeout and the hourly cache. `shortcut: sync httpx call, switch to a thread if a hot async path shows latency`.
- `warn` mode changes nothing for the live pod except a log line; `refuse` is opt-in.
- The optional env wiring is inert until keys exist, but adding keys without removing `GITHUB_TOKEN` crash-loops the pod at its next restart. The runbook states the order; there is no fix on the manifest side because a secretKeyRef cannot depend on another key.
- Argo CD syncs the gitops PR live; the change is inert by default.

## Verification

- Unit tests in `tests/test_github_env.py` (or a new file beside it) with a stubbed `_fetch_scopes`; `dotenv_key_blocked` is true for GitHub names under `refuse` and false under `warn`; and: admin scopes + `warn` passes and logs once without the token; `refuse` returns `{}`; `UNKNOWN` + `refuse` returns `{}`; `UNKNOWN` + `warn` passes; `ghs_`/`github_pat_` skip the call; unknown policy string behaves as `refuse`; cache means one fetch for repeated calls; the agent-token branch is checked too.
- `tests/test_github_app_setup.py` manifest permissions; docs build; ruff and the repo's pre-commit.
- Gitops: manifest-validate on the PR.
