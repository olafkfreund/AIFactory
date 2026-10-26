---
status: approved
issue: 1882
spec: spec/2026-10-26-1882-mcp-stdio-task-access.md
coders:
  1: codex
  2: codex
---

# Plan: per-task access check on the mcp-stdio write proxies (#1882)

Approved decisions (from the spec):

- `acw_` keys are user- and org-bound (`ApiKey.user_id`/`org_id` non-null,
  mint route checks membership), so the stdio write proxies must apply the
  same `member` check the REST routes apply.
- Reuse `routes.project_authz.authorize_project_for_user`; no new rules, no
  key-org confinement, read proxies unchanged, project-create unchanged.
- Principal = `server.auth._acw_principal(key, scoped_service_tokens_enabled=False)`
  for an `AuthenticatedKey`; `{"id": "default", "role": "user", "is_service": True}`
  for `_LegacyAdminKey`. Do not touch `request.state.user`.
- Project id = `task_id` path param before `:` (whole id if no `:`), else the
  `project_id` query param.
- Coders: codex via `codex-implement` (lane brief); fall back to doing the
  step by hand if it fails.

## Steps

1. `apps/web-server/server/mcp_stdio/auth.py` (after `require_acw_scope`, ~:141):
   add `require_acw_access(scope: str, minimum_role: str = "member")` returning
   `async def _check(request: Request, key=Depends(require_acw_scope(scope)), db: AsyncSession = Depends(get_db))`
   that builds the principal, derives the project id from
   `request.path_params.get("task_id")` / `request.query_params.get("project_id")`,
   awaits `authorize_project_for_user(principal, project_id, db, minimum_role)`
   (lazy import from `..routes.project_authz` to avoid an import cycle) and
   returns `key`. Update the module docstring's scope list to say write
   routes also enforce project membership.
   `apps/web-server/server/mcp_stdio/router.py`: import `require_acw_access`;
   use `Depends(require_acw_access(TASK_WRITE_SCOPE))` on create-and-run
   (:219), start (:247), stop (:267), recover (:286), approve-plan (:306) and
   `Depends(require_acw_access(TASK_MERGE_SCOPE))` on create-pr (:331), merge
   (:353). Add a docstring paragraph: acw keys are user/org-bound, write
   proxies apply the REST route's `member` check against the key owner;
   the legacy token is a service principal.
   → verify by `cd apps/web-server && ruff check server/mcp_stdio && ruff format --check server/mcp_stdio`
   and `pytest tests/test_mcp_stdio_proxy.py tests/test_mcp_stdio_create_and_run.py tests/test_mcp_task_control.py tests/audit/test_task_action_audit.py -q`.
   Traps: keep handler parameter names (`key`, `project_id`, `title`,
   `description`) — `tests/test_mcp_task_control.py:370` inspects the Query
   params and `tests/audit/test_task_action_audit.py:123` calls
   `proxy_stop_task(..., key=key)` directly. `get_db` lives in
   `server.database.engine`.
2. New `tests/test_mcp_stdio_task_access.py`: mount `server.mcp_stdio.router`
   on a FastAPI app; monkeypatch `server.routes.project_authz._auth_disabled`
   to `False` (CI sets `APP_DISABLE_AUTH=true`), `server.mcp_stdio.auth.mcp_remote_auth.authenticate`
   to return an `AuthenticatedKey` with both scopes, `server.project_registry.load_projects`
   to `{"p1": {"org_id": "org-a"}}`, override `get_db` with a fake session
   whose `execute()` result's `scalar_one_or_none()` returns an `OrgMember`
   (role `member`) only for the owner user, stub each delegated handler and
   `_audit_mcp_write`. Parametrise over the seven proxies: foreign key → 403
   and handler not called; owner key → 200 and handler called; unknown
   project `p9` → 404; legacy token (patch `API_TOKEN`) → 200.
   → verify by `pytest tests/test_mcp_stdio_task_access.py -q`, and confirm it
   fails with step 1 reverted (`git stash` is banned: check by temporarily
   swapping the dependency back by hand).
   Traps: package `server.mcp_stdio` re-exports `router` (the APIRouter),
   shadowing the module; import the module via `importlib.import_module`.

## Tests

`pytest tests/test_mcp_stdio_task_access.py tests/test_mcp_stdio_proxy.py tests/test_mcp_stdio_create_and_run.py tests/test_mcp_stdio_list_projects.py tests/test_mcp_task_control.py tests/audit/ -q`
→ all pass. ruff check/format clean on touched files.

## Rollback

Revert the PR commit; the dependency swap is the only behaviour change.

## Revision 1 (lead review of #1885)

Approved additions (spec "Revision 1"):

3. `apps/web-server/server/mcp_stdio/auth.py`: add public `acw_principal(key)`
   (legacy key → `{"id": "default", "role": "user", "is_service": True}`;
   else `_acw_principal(key, scoped_service_tokens_enabled=getattr(get_settings(),
   "SCOPED_SERVICE_TOKENS_ENABLED", False))`, `get_settings` from `server.config`);
   use it in `require_acw_access`; add `require_acw_principal(scope)` (scope
   check, then `request.state.user = acw_principal(key)`, return key).
   `router.py`: task-id read proxies → `require_acw_access(MCP_READ_SCOPE, "viewer")`;
   `/projects`, `/tasks`, `/tasks/running` → `require_acw_principal(MCP_READ_SCOPE)`
   plus `request` and `db: Annotated[AsyncSession, Depends(get_db)]`;
   `/tasks` calls `list_tasks(project_id=..., status=..., request=request, db=db)`;
   `/projects` and `/tasks/running` filter by `accessible_org_ids(request, db)`
   (None = all) on the project's `org_id` (running id → project = part before `:`).
   → verify by the strict ratchets (`scripts/cq_ratchet.py`, ruff and `--tool mypy`, base `origin/dev`)
   and the targeted pytest.
   Traps: new imports in router.py must be absolute and top-level, and every new
   `Depends` default needs `Annotated` (the strict ruff/mypy ratchet counts per
   file). A closure-local dependency can't go in `Annotated` under
   `from __future__ import annotations`, so use a default with `# noqa: B008`.
   `tests/test_mcp_stdio_list_projects.py` pins the unfiltered service list.
4. `tests/test_mcp_stdio_task_access.py`: parametrise the 4 task-id read proxies
   (foreign 403, owner 200, unknown 404, legacy 200); list tests: foreign key
   sees no `p1` project/task/running id, owner and legacy do.
   → verify by `pytest tests/test_mcp_stdio_task_access.py -q`.
5. After #1881 shows MERGED: `git merge origin/dev` (no force-push), swap the
   apply-correction proxy to `require_acw_access(TASK_WRITE_SCOPE)`, add it to
   the write parametrisation (`execution.apply_task_correction`).
   → verify by the targeted pytest + ratchets.

## Revision 2: post-deploy verification

Context: live `api_keys` has 0 rows, so all MCP callers use the legacy service
token and pass the check (spec "Revision 2").

6. After the release containing this PR is deployed, through the `aifactory`
   MCP server (legacy token, as configured today):
   - read: `task_list` (optionally with a known `project_id`) → succeeds and
     returns the same tasks as before the deploy.
   - write path: `task_stop` on an existing task that is **not** running →
     the request passes the new authz dependency and reaches the handler,
     which answers 404 `"Task is not running"`. That is the expected,
     harmless outcome: nothing is stopped or mutated. A failure would be
     401/403, or 404 `"Project not found"`, both from the authz layer.
     (No write proxy is both harmless and 2xx; create-and-run, start,
     recover, approve-plan and merge all mutate state.)
   Rollback trigger: either call fails at the authz layer → revert the PR.
