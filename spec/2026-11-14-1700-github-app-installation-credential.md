---
status: draft
issue: 1700
intent: intent/2026-11-14-1700-github-app-installation-credential.md
---

# Spec: per-tenant GitHub App installations

## Design

One App key, an operator-set map from org to installation id, and tokens
minted per installation and handed only to the call that needs them. When
the map is set the server is in **multi-installation mode**, which fails
closed: a call that does not name a bound org gets no GitHub token at all.
Paths are under `apps/web-server/server/` unless stated.

### 1. Operator binding: `AIFACTORY_GITHUB_APP_ORG_INSTALLATIONS`

A new env variable on the web server, e.g. `default=9001,acme=9002`: a
comma-separated list of `<org_id>=<installation_id>`. `github_app.start()`
(`services/github_app.py:143`) parses it once. `start()` raises when:

- the map is set but the App is not configured (it would silently do
  nothing);
- an entry has an empty org id, a duplicate org id, or an installation id
  that is not all ASCII digits (the message names the org id; nothing in the
  map is secret);
- the map is set together with `AIFACTORY_AGENT_GITHUB_TOKEN` (#1973): that
  token is one deployment-wide credential and would be handed to every
  tenant's runners.

`AIFACTORY_GITHUB_APP_INSTALLATION_ID` is validated the same way (ASCII
digits) before the boot mint, with or without the map; today it goes into
the API URL unchecked (`github_app.py:85`).

`github_app.multi_installation() -> bool`: the map is non-empty.
`github_app.installation_for_org(org_id) -> str | None`.

Nothing a tenant can edit feeds this map. The stored-credential kind
`github_app` stays reserved and rejected: a tenant-supplied installation id
would let one tenant act as another's installation, because the id is not a
secret and the server holds the key.

Chart: `githubApp.orgInstallations: {}` (org id → installation id, given as
strings) rendered into the env variable in
`charts/aifactory/templates/deployment.yaml` inside the existing
`githubApp.enabled` block. Values go through `toString` and the template
fails on anything that is not all digits, so a YAML number in scientific
notation cannot slip through.

### 2. Minting per installation

`_mint()` (`github_app.py:81`) takes the installation id as an argument and
returns the token and its expiry (parsed from `expires_at`; one hour from
now if absent or unparsable).

`github_app.installation_token(installation_id, *, min_ttl=900) -> str`:
returns the cached token for that installation if it has at least `min_ttl`
seconds left, else mints, caches and returns a new one. The cache is a
module dict behind a `threading.Lock`. The id is checked again here before
it goes into the URL. A mint failure raises; nothing falls back to another
token. A per-installation token is never written to `os.environ`.

In multi-installation mode the server does **not** publish a process-wide
token: `start()` validates and does one mint per bound installation to fail
early on a wrong id, but skips `_publish`, and `refresh_loop` does nothing.
Without the map, boot and refresh publish the default installation's token
exactly as today.

### 3. Handing the token to the web server's own git and gh calls

`apps/backend/core/git_credentials.py`: a `contextvars.ContextVar` holds an
optional token *provider* (a callable returning a token). `github_token()`
(line 42): when a provider is set it returns `provider().strip()` and never
looks at `os.environ`; otherwise it reads the env as today. New context
manager `use_github_token(provider)` sets the variable and always resets it
on exit. `github_env` and `authed_push_url` are unchanged: they call
`github_token()`, put the token in the per-call `hosts.yml`, and the
credential helper stays scoped to `https://github.com`.

A provider rather than a token string, because the PR endgame's watcher
runs for 20 minutes and longer; each `gh` call asks the provider, and the
cache hands out a token with at least 15 minutes left.

`github_app.org_github_token(org_id)` is the context manager the call sites
use:

- not multi-installation mode: a no-op (ambient token, today's behaviour);
- multi-installation mode, bound org: `use_github_token(lambda:
  installation_token(id))`;
- multi-installation mode, unbound org or `None`: `use_github_token(lambda:
  "")`, so the call runs with no GitHub token.

It always sets the variable explicitly in multi-installation mode, so a
nested block for another org can never inherit the outer org's provider.
`asyncio.to_thread` and `asyncio.create_task` copy the context at the call,
so threads and tasks started inside the block keep the provider; tasks
started before it do not get it.

The mint is a blocking HTTP call. Async call sites warm the cache first
with `await asyncio.to_thread(github_app.token_for_org, org_id)`
(`token_for_org` returns `None` outside multi-installation mode or for an
unbound org) so the provider answers from the cache on the event loop.

### 4. Finding a project's org

`project_registry.org_id_for_path(project_path) -> str | None`: the `org_id`
of the registry entry (`load_projects()`, `project_registry.py:47`) whose
resolved `path` equals the resolved `project_path` or is a parent of it (a
task worktree lives under its project). No match returns None, which in
multi-installation mode means no token.

### 5. Call sites wired (the autonomous pipeline)

| Step | Where | Change |
| --- | --- | --- |
| Clone at project add | `routes/projects.py:563-582` | resolve the owner org before the clone (the code at `:649-653` moves up); run `clone_or_update` inside `org_github_token(org_id)`, with or without a stored credential |
| Runner spawn (build, spec, PR review) | `utils/subprocess_env.py` `runner_github_env` (from #1973) | new argument `project_path`; in multi-installation mode it returns the org's installation token minted with `min_ttl=3300` under the three fixed names, or nothing for an unbound org or unknown path; the ambient names and the agent token are not used. Outside the mode it is unchanged |
| Build Job env | `services/build_backend.py:1332` → `build_job_env` | pass the project path through to `runner_github_env` |
| PR endgame (context, push, create, review request, watch, merge) | `services/completion_orchestration.py`, from the context gathering at `:294` through `run_pr_endgame` at `:518` | run inside `org_github_token(org_id_for_path(project_path))` |

`make_subprocess_env(github=True, project_path=...)` passes the path on; the
three `github=True` callers from #1973 (`agent_service.py:1110`,
`agent_spec_creation.py:223`, `pr_review_service.py:258`) supply it. In
multi-installation mode `github=True` without a path yields no token.

A runner's token is fixed at spawn: at least 55 minutes from the moment the
env is built (Job scheduling time comes out of that). A longer build fails
its final push, as today (`github_app.py:174`).

Not wired, and therefore **without a GitHub token in multi-installation
mode**: the interactive GitHub routes (`routes/github.py`, `routes/pr.py`,
`routes/git.py`), `services/pr_data_service.py`,
`services/copilot_dispatch_service.py`, `services/task_branch.py`,
`services/merger.py`, `services/gh.py`, and the control plane's worktree
fetch in `services/build_backend.py:1018`. They all go through `github_env`,
so each needs only the `org_github_token` wrapper once its project is known
at the call. Their `gh` and `git` calls fail as unauthenticated until then.
The docs list them, and the issue stays open for them.

### 6. Docs

`docs/docs/concepts/github-app.md`: section "Several installations": the
map, that only the operator sets it, that every org including `default`
must be bound, what is wired and what has no token in this mode, that it
cannot be combined with the agent token, and the token lifetime.

## Alternatives rejected

- **Stored `github_app` credential holding the installation id** (the
  issue's original wording). Any org member can create credentials
  (`routes/git_credentials.py:142`), and `_resolve_git_credential` looks up
  by id alone (`routes/projects.py:338`). With a non-secret id that is a
  confused-deputy hole. An operator-set map has no tenant-writable input.
- **Verify ownership through GitHub.** AIFactory has no trusted mapping from
  its org to a GitHub account to compare against.
- **A project setting for the installation id.** Tenant-editable.
- **Keep the default installation as the fallback for unbound orgs and
  unwired routes.** Then an unbound tenant, or a bound tenant through an
  unwired route, reaches the default installation's repos. Failing closed
  costs the interactive routes in this mode, which no deployment uses yet.
- **Publish per-tenant tokens to `os.environ`.** A concurrent request for
  another tenant would use it.
- **Pass a token argument through every `github_env` call site.** About
  twenty call sites; the context variable reaches them through the one
  function they already call.
- **Per-tenant App keys.** Rejected by the owner (2026-10-17, 2026-10-31).

## Risks

- Tenant mix-up through the context. Mitigation: the provider is always set
  explicitly in multi-installation mode, from the resolved owner org or
  `org_id_for_path` only; tests cover nested and interleaved orgs, reset
  after an exception, and a task that outlives the block.
- A path that is wired outside the mode but forgotten inside it fails
  closed (no token), not open.
- Turning the mode on disables the interactive GitHub routes until they are
  wired. Documented; the mode is off by default.
- A build longer than about 55 minutes fails its final push.
- `org_id_for_path` reads `projects.json` on each spawn and endgame; the
  file is small and already read on these paths.
- Depends on #1973's `runner_github_env`; this branch is built on it.

## Verification

All with a mocked GitHub API (`httpx.post` patched); no App needed.

- `apps/web-server/tests/test_github_app.py`: map parsing (valid; duplicate
  org; empty org; non-numeric id; map without the App; map with the agent
  token → raise, each before any HTTP call); a non-numeric default
  installation id raises before any HTTP call; `installation_token` caches
  per installation, re-mints under `min_ttl` and at expiry, handles a
  missing or malformed `expires_at`, propagates a mint error, rejects a bad
  id before any HTTP call, never touches `os.environ`; in
  multi-installation mode `start()` publishes nothing and `refresh_loop`
  publishes nothing; without the map both behave as today.
- `tests/test_github_env.py`: with a provider, `github_env` writes the
  provider's token and ignores `os.environ`; an empty provider yields the
  base env with no token; reset on exit and after an exception; nested A →
  unbound and A → B; two interleaved asyncio tasks for two orgs each see
  their own token in the child's `hosts.yml`; a thread and a task started
  inside the block keep the provider after the block exits; a task started
  before it does not get it.
- `runner_github_env(project_path)`: bound org → its token under the three
  names; unbound org, unknown path and missing path → empty in the mode;
  unchanged outside the mode.
- `org_id_for_path`: exact path, a worktree under the project, no match.
- Clone: the git child of a bound org's project add gets that org's token,
  with and without a stored credential; an unbound org gets none in the
  mode.
- Endgame: `run_pr_endgame` for a bound org's project runs `gh` with that
  org's token, including the background watcher.
- Helm: the env renders from `githubApp.orgInstallations` only when the App
  is enabled; a non-digit value fails the render; an 8-digit id renders as
  digits.
- `pytest apps/web-server/tests/test_github_app.py tests/test_github_env.py
  tests/test_child_process_env.py tests/helm/test_github_app_toggle.py`,
  the existing endgame tests, and `scripts/cq_ratchet.py` on touched files.
- Not verified: a real second installation (needs the owner's App and a
  second account).
