---
status: draft
issue: 1700
intent: intent/2026-11-16-1700-wire-org-token-routes.md
---

# Spec: wire the remaining GitHub paths to per-org installation tokens

Paths are under `apps/web-server/server/` unless they start with `apps/`,
`tests/`, `charts/` or `docs/`.

## Design

Every unwired path already builds its subprocess env with
`github_env(child_env())`, which reads the ambient token through
`core.git_credentials.github_token()`. #2039 made that read go through a
`ContextVar` provider. So wiring a path means setting the right provider
around it; no call site learns about tokens.

**One helper, two entry points** (in `services/github_app.py`):

- `project_github_token(path)`: a context manager. Outside the mode it does
  nothing and reads no file. In the mode it resolves
  `org_id_for_path(path)` and enters `org_github_token(org)`. One exception:
  when NO path was supplied (`None` or empty) and an outer block already set
  a provider (the PR endgame, a request dependency), it keeps the outer one,
  so an inner `cwd=None` call does not wipe a bound org's token. An explicit
  path that resolves to no org (unregistered, ambiguous overlap, unreadable
  registry) always gets the empty provider, even inside an outer block: the
  outer org's token never reaches a path that org does not own. With no path
  and no outer provider it also sets the empty provider (fail closed).
- `project_github_env(path, base_env)` in `utils/subprocess_env.py`:
  `project_github_token(path)` plus `github_env(base_env)`. The sites below
  swap `github_env(child_env())` for `project_github_env(cwd, child_env())`.
  `apps/backend` and its CLI keep plain `github_env`.

**Low-level sites that have a path** (swap to `project_github_env`):
`services/gh.py` (`cwd`), `services/pr_data_service.py` `_run_gh` (`cwd`),
`services/task_branch.py` `_git` (`cwd`), `routes/git.py` `run_git_command`
and `run_gh_command` (`cwd`), `routes/pr.py` (three sites, the task
worktree), `services/pr_endgame.py` `_default_runner` (`cwd`),
`services/build_backend.py` `_git` (the path after `-C`; the one call
without `-C`, the local `git clone --local` of the project, needs no token).
`services/merger.py` `process_one` wraps its body, and `sweep` wraps each
`_process_spec` call (inside its project loop, so each project gets its own
org), in `project_github_token(project_path)`, because the merger's runner
passes a `None` cwd for `gh pr list --repo`. The Auto-Fix pull
(`services/auto_fix_service.py::_pull_clone_if_any`, which calls
`clone_or_update` with no outer provider) wraps in
`project_github_token(project path)` after a threaded warm-up.

**Project routes in `routes/github.py`** call `gh` with `--repo` and often no
`cwd`. A router-level async dependency `project_github_dependency`
(`routes/project_authz.py` is not touched) reads `projectId` from the path or
query, looks up that project's own `org_id`, warms the token in a thread (one
cache fill, so the handler's later reads are cache reads), then yields inside
`org_github_token(org)`. It is an `async` generator on purpose: the set and
the reset must run in the handler's context. A caller-supplied `projectId`
must not select credentials by itself: in the mode the dependency first runs
the project access check (`ProjectAccessChecker("viewer")._authorize`); a
caller who fails it, or names no project, gets the empty provider and the
handler proceeds as before (no 403 is added). `_persist_cli_token_to_project`
returns False in the mode: an installation token is never written into a
project's settings. Outside the mode the dependency yields at once. Routes with no project (login, `auth/status`, user, orgs, models) get
the empty provider in the mode, as today.

**Copilot dispatch** (`routes/execution.py`): `_resolve_task` already returns
the project path; wrap the dispatch thread call and the `spawn(watch...)` in
`project_github_token(project_path)`. The watcher's task copies the context,
and its polls re-read the cached token and re-mint through the provider.

**Spec Jobs** (`services/spec_job_backend.py`): no code change. It calls
`build_job_env(oauth_token)` and then pops `GITHUB_TOKEN`/`GH_TOKEN`, in and
out of the mode; Kubernetes spec Jobs have never had a GitHub token. The docs
stop listing them as unwired.

**#2038 items**

1. Overlap: `project_registry.overlaps_other_org(path, org_id, projects,
   skip=None)`: true when the normalised path equals, contains or is
   contained by another entry's path and that entry's `org_id` differs. Called
   in `add_project` and in `update_project` (repoint), only when
   `multi_installation()`. In `add_project` it runs BEFORE any filesystem or
   git mutation: in local mode right after the root confinement and before
   the `mkdir`; in clone mode on `workspace_root() / slug_from_git_url(gitUrl)`
   before `clone_or_update`, which would otherwise fetch, pull and reset
   another org's working copy first. A refusal is
   a 409 that does not name the other project or org (no cross-tenant leak).
   Same-org overlap stays allowed.
2. Event loop: `gather_pr_context` (sync git/gh reads) moves behind
   `asyncio.to_thread`; `run_pr_endgame`'s other reads already do. Context
   variables follow `to_thread`, so the provider is set there.
3. Warm-up: the `token_for_org` warm-up moves inside the
   `is_auto_pr_enabled` branch.
4. `_parse_org_map`: separate messages for a bad or duplicate org and for a
   bad installation id; the latter names the org, never echoes the id.
5. Chart: `deployment.yaml` fails the render for an org key that does not
   match `^[^,=[:space:]]+$`, naming the key; a Helm test covers it.

## Alternatives rejected

- Pass a token or org argument through every call: touches dozens of
  signatures and every caller; the ContextVar already exists for this.
- Resolve the org inside core `github_env`: `apps/backend` has no project
  registry, and the CLI must not depend on the server.
- Per-route `Depends` on each of ~26 handlers: easy to miss one; the router
  level covers new routes too.
- Reject overlaps in every deployment: would change today's single-tenant
  behaviour. The check only matters when tokens are per org.
- Make `org_id_for_path` pick the longest match instead of denying overlaps:
  silently changes a security decision; the deny is by design.

## Risks

- The dependency adds a registry read per request on `github.py` routes in the
  mode; none outside it.
- A blocking `run_gh_command` already runs on the loop in async handlers;
  the warm-up keeps the token read from adding a mint to that.
- A block that outlives its warmed token (about 14 minutes) can still mint
  inline once; only the endgame's long-lived block matters and its reads are
  now in threads.
- Registering an org's project inside another org's directory now fails in the
  mode; that is the intent.

## Verification

- Unit tests per site: with the mode on and two orgs, the right org's token
  reaches `gh`/`git` (assert the `hosts.yml` token or patch `github_token`),
  an unbound or unknown path gets none, the nested-keep rule holds, and
  without the map behaviour and the token-source are unchanged.
- Dependency test through `TestClient`: handler sees the org's provider for
  `projectId`, and the context is reset after the response.
- Overlap tests for add and update; parse-message tests; endgame test that
  warm-up does not run with auto-PR off and `gather_pr_context` runs off the
  loop thread.
- `helm template` tests for the key check.
- Repo lint (`ruff`) and the touched test modules; the CI `unit` set.
- Owner action (not done here): the live check with a second installation.
