---
status: approved
issue: 1700
spec: spec/2026-11-16-1700-wire-org-token-routes.md
coders:
  1: lead    # small, security-sensitive core; lead keeps it
  2: lead    # one-line swaps at ~12 sites; faster than a handoff
  3: lead    # depends on the step 1 API and tenant isolation
  4: lead    # #2038 fixes; small and unrelated files
  5: lead    # docs and plan deviations
reviewers:
  - codex    # independent read of the diff against this plan
  - agy      # second independent read
---

# Plan: wire the remaining GitHub paths to per-org installation tokens

Refs #1700 (olafkfreund/AIFactory#1700) and #2038. Builds on #2039
(per-org installation tokens: `github_app.org_github_token`,
`core.git_credentials.use_github_token`, `project_registry.org_id_for_path`).

## Approved decisions (from the spec)

- Wiring = set the right token provider around a path; no call site handles
  tokens. Two helpers: `github_app.project_github_token(path)` (context
  manager) and `subprocess_env.project_github_env(path, base_env)`.
- `project_github_token(path)`: no-op and no file read outside the mode. In
  the mode: if `path` is None or empty and an outer provider is set, keep the
  outer one; otherwise `org_github_token(org_id_for_path(path))` (an explicit
  path with no owner, even inside an outer block, gets the empty provider:
  fail closed).
- Low-level sites that have a path swap `github_env(child_env())` for
  `project_github_env(cwd, child_env())`: `services/gh.py`,
  `services/pr_data_service.py::_run_gh`, `services/task_branch.py::_git`,
  `routes/git.py` (`run_git_command`, `run_gh_command`), `routes/pr.py`
  (3 sites), `services/pr_endgame.py::_default_runner`,
  `services/build_backend.py::_git` (path after `-C`).
  `services/merger.py` wraps `process_one`'s body and each `_process_spec`
  call inside `sweep`'s project loop in `project_github_token(project_path)`.
  `services/auto_fix_service.py::_pull_clone_if_any` wraps `clone_or_update`
  the same way after a threaded warm-up (`github_app.warm_org_token`).
- `routes/github.py`: an async-generator dependency on its routers reads
  `projectId` (path or query), runs the project access check (viewer), and
  only for an authorised caller takes that project's own `org_id`, warms the
  token in a thread and yields inside `org_github_token(org)`; anyone else
  gets the empty provider and the handler proceeds. No-op outside the mode.
  `_persist_cli_token_to_project` returns False in the mode.
- Copilot dispatch (`routes/execution.py`): wrap dispatch and the watcher
  spawn in `project_github_token(project_path)`.
- Spec Jobs: no code change (they pop the token in and out of the mode); the
  docs say so.
- #2038: reject overlap of another org's project path at project add (before
  any mkdir or clone: local mode after confinement, clone mode on
  `workspace_root()/slug_from_git_url(gitUrl)`) and repoint (mode only, 409
  that names nothing of the other org); endgame
  `gather_pr_context` in `asyncio.to_thread`; warm-up only inside
  `is_auto_pr_enabled`; split `_parse_org_map` messages; chart rejects org
  keys not matching `^[^,=[:space:]]+$`.
- Without the map nothing changes.

Paths are under `apps/web-server/server/` unless they start with `apps/`,
`tests/`, `charts/` or `docs/`.

## Steps

1. Helpers (lead).
   - `services/github_app.py` (after `org_github_token`, ~`:100-125`): add
     `project_github_token(path)` as decided (lazy import of
     `server.project_registry`).
   - `apps/backend/core/git_credentials.py` (next to `use_github_token`, ~`:47`):
     add `has_github_token_provider() -> bool`.
   - `utils/subprocess_env.py` (after `runner_github_env`): add
     `project_github_env(path, base_env)`; lazy-import `github_app` (cycle).
     Export it in `__all__`.
   - `services/github_app.py`: also add `async warm_org_token(org_id)`
     (`asyncio.to_thread(token_for_org, org_id)`, logs `MINT_ERRORS` at
     warning level, never raises); the endgame, the dependency and Auto-Fix
     use it.
   - `services/github_app.py::_parse_org_map` (`:63-70`): empty or duplicate
     org id -> "invalid or duplicate org id"; bad installation id ->
     "invalid installation id for org <org>" (never the id).
   Check: `apps/backend/.venv/bin/pytest apps/web-server/tests/test_github_app.py -q -o asyncio_mode=auto`.
   Traps: `apps/backend` must not import `apps/web-server`; never log a token.

2. Low-level swaps (lead): the sites listed above; for `build_backend._git`
   take `args[1]` when `args[0] == "-C"`; `merger.process_one` and the sweep
   (`:458-560`) wrap in `project_github_token`.
   Check: `apps/backend/.venv/bin/pytest apps/web-server/tests -q -o asyncio_mode=auto -k "gh or pr_data or task_branch or merger or pr_endgame or build_backend or github_env"`.
   Traps: the cq ratchet counts ruff and mypy findings per touched file; keep
   `# noqa` lines; use plain imported names.

3. Routes (lead).
   - `services/auto_fix_service.py::_pull_clone_if_any` (`~:589`): wrap.
   - `routes/github.py`: add `project_github_dependency` (before `router`
     at `:34`) and pass `dependencies=[Depends(...)]` to `router` and
     `project_router` (`:34`, `:891`).
   - `routes/execution.py` (`:1649-1700`): wrap per the decision.
   Check: new tests in `apps/web-server/tests/test_org_installation_wiring.py`
   (dependency through `TestClient`, reset after response; copilot dispatch
   sees the org provider).

4. #2038 (lead).
   - `project_registry.py`: add `overlaps_other_org`.
   - `routes/projects.py`: call it in `add_project` (before the `mkdir` and
     before `clone_or_update`, see decisions) and `update_project` (`~:726`),
     mode only, 409.
   - `services/completion_orchestration.py` (`:285-310`): warm-up inside
     `is_auto_pr_enabled`; `ctx = await asyncio.to_thread(gather_pr_context, ...)`.
   - `charts/aifactory/templates/deployment.yaml` (`:683-688`): key regex.
   - Tests: `apps/web-server/tests/test_project_registry_org.py` (overlap),
     `apps/web-server/tests/test_org_installation_wiring.py`,
     `tests/helm/test_github_app_toggle.py`
     (bad key), the existing completion orchestration tests.
   Check: the touched test modules; `helm` tests run only when helm exists.

5. Docs (lead): `docs/docs/concepts/github-app.md` "Several installations":
   replace the unwired list with what is still unwired (routes with no
   project: login, user, orgs, models; the spec Jobs, by design); add the
   overlap rule; this plan's Deviations section gets any deviation.

## Tests

- `apps/backend/.venv/bin/pytest <touched modules> -q -o asyncio_mode=auto`
  all green; `ruff check` and `ruff format --check` on touched files.
- Final: `codex review --base origin/dev`, agy, own read against this plan.

## Rollback

Unset `AIFACTORY_GITHUB_APP_ORG_INSTALLATIONS` (chart
`githubApp.orgInstallations={}`) and restart: nothing here runs outside the
mode except the chart key check and `_parse_org_map` wording. Reverting the PR
removes the code.

## Deviations

None yet.
