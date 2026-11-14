---
status: draft
issue: 1700
intent: intent/2026-11-14-1700-github-app-installation-credential.md
---

# Spec: per-tenant GitHub App installations

## Design

One App key, an operator-set map from org to installation id, and tokens
minted per installation and handed to the call that needs them. Paths are
under `apps/web-server/server/` unless stated.

### 1. Operator binding: `AIFACTORY_GITHUB_APP_ORG_INSTALLATIONS`

A new env variable on the web server, e.g. `acme=9001,globex=9002`: a
comma-separated list of `<org_id>=<installation_id>`. `github_app.start()`
(`services/github_app.py:143`) parses it once.

- It is only read when the App is configured. Set without the App,
  `start()` raises (the map would silently do nothing).
- An entry with an empty org id, a duplicate org id, or an installation id
  that is not all ASCII digits makes `start()` raise. The error names the
  org id, never a secret (there is none in the map).
- `github_app.installation_for_org(org_id) -> str | None` returns the bound
  id, or None for an unbound org (which keeps the default installation).

Nothing a tenant can edit feeds this map. The stored-credential kind
`github_app` stays reserved and rejected: a tenant-supplied installation id
would let one tenant act as another's installation, because the id is not a
secret and the server holds the key.

Chart: `githubApp.orgInstallations: {}` (map) rendered into the env variable
in `charts/aifactory/templates/deployment.yaml` inside the existing
`githubApp.enabled` block.

### 2. Minting per installation

`_mint()` (`github_app.py:79`) takes the installation id as an argument and
returns the token and its expiry (`expires_at` in GitHub's answer; one hour
from now if it is missing). The boot mint and `refresh_loop` call it with
the default id and keep publishing to `os.environ` as today.

New `github_app.installation_token(installation_id, *, min_ttl=900) -> str`:
returns a cached token for that installation if it has at least `min_ttl`
seconds left, else mints, caches and returns a new one. The cache is a
module dict guarded by a `threading.Lock` (callers run in worker threads).
The id is checked again here (`isascii()` and `isdecimal()`) before it goes
into the API URL. A per-installation token is never written to `os.environ`.

`github_app.token_for_org(org_id, *, min_ttl=900) -> str | None`: None when
the App is not configured or the org is unbound; else
`installation_token(...)`.

### 3. Handing the token to the web server's own git and gh calls

`apps/backend/core/git_credentials.py`: `github_token()` (line 42) first
consults a `contextvars.ContextVar` holding a token *provider* (a callable
returning a token). New context manager `use_github_token(provider)` sets
and resets it. `github_env` and `authed_push_url` are unchanged: they call
`github_token()`, put the token in the per-call `hosts.yml`, and the
credential helper stays scoped to `https://github.com`.

A provider, not a token string, because the PR endgame's watcher runs for
20 minutes and longer; each `gh` call asks the provider, and the cache in
section 2 hands out a token with at least 15 minutes left.

`github_app.org_github_token(org_id)` is the context manager the call sites
use: a no-op for None or an unbound org, else
`use_github_token(lambda: installation_token(id))`. `asyncio.to_thread` and
`asyncio.create_task` copy the context, so threads and background tasks
started inside the block keep the provider.

### 4. Finding a project's org

`project_registry.org_id_for_path(project_path) -> str | None`: the `org_id`
of the registry entry whose `path` equals the resolved `project_path`
(`load_projects()`, `project_registry.py:47`), else None.

### 5. Call sites wired (the autonomous pipeline)

| Step | Where | Change |
| --- | --- | --- |
| Clone at project add | `routes/projects.py:563-582` | resolve the owner org before the clone (the code at `:649-653` moves up); run `clone_or_update` inside `org_github_token(org_id)` when no stored credential is given |
| Runner spawn (build, spec, PR review) | `utils/subprocess_env.py` `runner_github_env` (from #1973) | new optional argument `project_path`; order: agent token (#1973), else the org's installation token minted with `min_ttl=3300` (a fresh hour for the build), else the ambient names |
| Build Job env | `services/build_backend.py:1332` → `build_job_env` | pass the project path through to `runner_github_env` |
| PR endgame (push, create, review request, watch, merge) | `services/completion_orchestration.py:518` | run `run_pr_endgame` inside `org_github_token(org_id_for_path(project_path))` |

`make_subprocess_env(github=True, project_path=...)` passes the path on; the
three `github=True` callers from #1973 supply it.

Not wired in this change, and listed in the docs as still using the default
installation: the interactive GitHub routes (`routes/github.py`,
`routes/pr.py`, `routes/git.py`), `services/pr_data_service.py`,
`services/copilot_dispatch_service.py`, `services/task_branch.py`,
`services/merger.py`, `services/gh.py`, and the control plane's worktree
fetch in `services/build_backend.py:1018`. They all go through
`github_env`, so each needs only the `org_github_token` wrapper once its
project is known at the call. The issue stays open for them.

### 6. Docs

`docs/docs/concepts/github-app.md`: section "Several installations": the
map, who may set it, what is and is not wired, the unbound-org limit from
the intent, and that per-installation tokens last an hour from the spawn.

## Alternatives rejected

- **Stored `github_app` credential holding the installation id** (the
  issue's original wording). Any org member can create credentials
  (`routes/git_credentials.py:142`), and `_resolve_git_credential` looks up
  by id alone (`routes/projects.py:338`). With a non-secret id that is a
  confused-deputy hole. An operator-set map has no tenant-writable input.
- **Verify ownership through GitHub** (compare the installation's account
  with the org). AIFactory has no trusted mapping from its org to a GitHub
  account to compare against.
- **A project setting for the installation id.** Project settings are
  tenant-editable.
- **Publish per-tenant tokens to `os.environ`.** A concurrent request for
  another tenant would use it.
- **Pass a token argument through every `github_env` call site.** About
  twenty call sites; the context variable reaches them all through the one
  function they already call.
- **Per-tenant App keys.** Rejected by the owner (2026-10-17, 2026-10-31).

## Risks

- Tenant mix-up if the context is set wrongly. Mitigation: the provider is
  set only from `org_id_for_path` or the resolved owner org, in three places;
  tests assert that two interleaved orgs get their own tokens and that the
  context is reset after the block.
- A build longer than an hour still fails its final push (the existing
  limit, `github_app.py:174`), now counted from the spawn.
- The unwired routes act as the default installation for a bound org's
  project. They fail with 404 from GitHub rather than leak, since the
  default installation does not cover that tenant's repos; the reverse (an
  unbound tenant reaching the default installation's repos) is the known
  limit in the intent.
- `org_id_for_path` reads `projects.json` on each spawn and endgame; the
  file is small and already read on these paths.
- Depends on #1973's `runner_github_env`; this branch is built on it.

## Verification

All with a mocked GitHub API (`httpx.post` patched); no App needed.

- `apps/web-server/tests/test_github_app.py`: map parsing (valid, duplicate
  org, non-numeric id, map without the App → raise); `installation_token`
  caches per installation, re-mints under `min_ttl`, rejects a non-numeric
  id before any HTTP call, never touches `os.environ`; two installations get
  different tokens.
- `tests/test_github_env.py`: `use_github_token` makes `github_env` write the
  provider's token; resets on exit; a thread started inside sees it; without
  it the ambient token is used.
- `runner_github_env(project_path)`: order agent token > org token > ambient.
- Clone and endgame: tests that the `gh`/`git` child of a bound org's
  project gets that org's token in `hosts.yml`, and an unbound org's project
  gets the default.
- Helm: the env renders from `githubApp.orgInstallations` only when the App
  is enabled.
- `pytest apps/web-server/tests/test_github_app.py tests/test_github_env.py
  tests/test_child_process_env.py tests/helm/test_github_app_toggle.py`,
  the existing endgame tests, and `scripts/cq_ratchet.py` on touched files.
- Not verified: a real second installation (needs the owner's App and a
  second account).
