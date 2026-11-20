---
status: draft
issue: 2181
spec: spec/2026-11-20-2181-aif-project-authz.md
coders:
  1: codex
  2: codex
  3: agy
  4: agy
  5: codex
  6: agy
reviewers:
  - codex
  - agy
---

# Plan: Enforce `require_project_role` across REST, WebSocket, and MCP surfaces

Self-contained summary of approved decisions:
- `require_project_role(principal, project_id, min_role, db=...)` enforces `viewer < member < admin < owner` (`ROLE_LEVELS`), returning effective access (`min(team_grant_role, scope_ceiling)`).
- Raises **404** when a project is unknown OR not visible to the principal (`team_grant_role is None` or `scope_ceiling is None`), and **403** when visible (`>= viewer`) but `effective_role < min_role`.
- Scope ceiling: read scopes (`mcp:read`, `*:read`) -> `viewer`; write scopes (`project:write`, `task:write`, `plan:write`, `mcp:write`, `api`) -> `member`; merge/admin scopes (`task:merge`, `*:admin`, `admin`) -> `admin`. User sessions (OIDC/JWT without `scopes`) use team grants only.
- List endpoints return only projects with `>= viewer`.
- `APP_API_TOKEN` keeps full access only while `Settings.LEGACY_SHARED_TOKEN_FULL_ACCESS` (`APP_LEGACY_SHARED_TOKEN_FULL_ACCESS`, default `True`) is enabled and logs a deprecation warning on each use. Scoped `acw_` keys never bypass. Single-user/local mode seeds one default team with the `default` user as `owner`.

### Coder assignment rationale

- `codex` (steps 1, 2, 5): Small, security-critical auth/principal/grant intersection logic and subtle indirect resolvers (WebSockets, MCP, path-to-project confinement).
- `agy` (steps 3, 4, 6): Large mechanical route sweeps across sub-routers and the generated route-table + matrix test suite.

## Steps

1. `apps/web-server/server/config.py:151-175`, `apps/web-server/server/auth.py:235-248,389-520`, `apps/web-server/server/mcp_stdio/auth.py:110-205`, `apps/web-server/server/database/engine.py:266-368`, `apps/web-server/server/oidc/provisioning.py:82-161`: Add `LEGACY_SHARED_TOKEN_FULL_ACCESS: bool = True`; mark `APP_API_TOKEN` principals with `is_legacy_shared_token=True` and deprecation log; always populate `scopes`, `org_id`, and `scoped_service` (with `is_service=False`) on `acw_` principals across HTTP, WebSocket, and `mcp_stdio`; seed/backfill `TeamMembership` on the org's `is_default=True` team (`default` user as `owner` in local mode) → verify by `apps/backend/.venv/bin/pytest apps/web-server/tests/test_scoped_service_tokens.py -v`
   Traps: Use absolute `server.*` imports (`TID252`); no `as` aliases inside parenthesised imports; naive UTC timestamps for Postgres compatibility.

2. `apps/web-server/server/routes/project_authz.py:1-353`: Implement `scope_ceiling(scopes)`, team-grant resolution via `TeamMembership` on `Project.team_id` (with default-team fallback), and `require_project_role(principal, project_id, min_role, db=...)` returning `404` when unknown/invisible and `403` when visible (`>= viewer`) with insufficient role/scope; alias `ProjectAccessChecker` / `authorize_project_for_user` / `accessible_project_ids` onto `require_project_role`; raise minimum role to `"admin"` on `worktree/merge` (`routes/worktree_merge.py:1814`) → verify by `apps/backend/.venv/bin/pytest apps/web-server/tests/test_authz_releases_session_1963.py tests/postgres/test_authz_denial_audit_pg.py -v`
   Traps: Must `await db.rollback()` before `_audit_authz_denial` and after successful checks so requests never hold two pooled connections (#1919, #1963); use `await aload_projects()` on async paths.

3. `apps/web-server/server/routes/changelog.py:143-1213`, `apps/web-server/server/routes/context.py:85-951`, `apps/web-server/server/routes/files.py:987-1052`, `apps/web-server/server/routes/mcp.py:77-150`, `apps/web-server/server/routes/project_github.py:14-50`, `apps/web-server/server/routes/github.py:507-897,2181,2430-2460`, `apps/web-server/server/routes/git.py:78-175`: Wire `require_project_role` into all `/api/projects/{projectId}/*` sub-routers (`viewer` for reads, `member` for writes, `admin` for `/env`, PR merge, release creation, token persistence, and `/github/remote`), make `project_github_token` enforce project role even when `github_app.multi_installation()` is false, and check `project_id_for_path(path)` on `/api/git/*` and `/api/github/*` path routes → verify by `apps/backend/.venv/bin/pytest apps/web-server/tests/ -k "github or git or context" -v`
   Traps: `scripts/cq_ratchet.py` complexity ceilings (`PLR0912`/`PLR0915`) — attach router-level or `Depends(...)` guards instead of bloating route bodies.

4. `apps/web-server/server/routes/tasks.py:123-343`, `apps/web-server/server/routes/execution.py:301-306,1568-1605`, `apps/web-server/server/routes/worktree_tools.py:259-330`, `apps/web-server/server/routes/stale.py:171-196`, `apps/web-server/server/routes/files.py:259-570`: Enforce `require_project_role` on `POST /api/tasks` (`member`), `GET /api/tasks?project_id=...` (uniform 404 when invisible), `POST /api/tasks/from-plan` (after `resolve_project_id`), `POST /api/tasks/worktree/open-in-(ide|terminal)` (`member` via `project_id_for_path`), `GET /api/files/(read|serve|list|discover)` (`viewer` on `project_id_for_path`, authenticating `acw_` keys in `_validate_serve_token` and filtering invisible project dirs), and filter `GET /api/tasks/running` and `/api/maintenance/stale-tasks*` by `accessible_project_ids` → verify by `apps/backend/.venv/bin/pytest apps/web-server/tests/test_path_containment.py -v`
   Traps: Do not touch vendored `apps/web-server/factory_common/`.

5. `apps/web-server/server/pty/access.py:17-55`, `apps/web-server/server/routes/terminal.py:70-476`, `apps/web-server/server/websockets/task_access.py:29-141`, `apps/web-server/server/websockets/events.py:140-260`, `apps/web-server/server/mcp_remote/tools.py:89-180`, `apps/web-server/server/routes/copilot_mcp.py:180-210,324-386`: Update terminal (`member` for create/mutate/WS shell, `viewer` for session reads), WebSocket task/event streams (remove `self.user is None` bypass when `LEGACY_SHARED_TOKEN_FULL_ACCESS` is false), `mcp_remote` (`admin` for `merge_pr`), and `copilot_mcp` (`project_id_for_path` check after `_resolve_task`) → verify by `apps/backend/.venv/bin/pytest apps/web-server/tests/test_mcp_remote.py apps/web-server/tests/test_ws_CrossOrg_authz.py -v`
   Traps: Keep WebSocket close codes consistent (`4401` unauthenticated, `4403`/`4404` unauthorized).

6. `apps/web-server/tests/test_project_authz_route_table.py`, `apps/web-server/tests/test_ws_CrossOrg_authz.py`, `apps/web-server/tests/test_scoped_service_tokens.py`: Add the generated route-table walker test (inspecting REST `app.routes`, WebSockets, and MCP tool definitions against an explicit `NON_PROJECT_ROUTES` allowlist) plus matrix tests for 404 unknown/invisible, 403 low role/scope, `team_grant ∩ scope_ceiling`, cross-team service key denial, and `APP_LEGACY_SHARED_TOKEN_FULL_ACCESS=false`; update legacy 403 assertions on invisible single-project routes to 404 → verify by `apps/backend/.venv/bin/pytest apps/web-server/tests/test_project_authz_route_table.py apps/web-server/tests/test_ws_CrossOrg_authz.py apps/web-server/tests/test_scoped_service_tokens.py -v && python3 scripts/cq_ratchet.py`
   Traps: Run `python3 scripts/cq_ratchet.py` and verify `git diff apps/web-server/factory_common/` is empty.

## Tests

```bash
apps/backend/.venv/bin/pytest apps/web-server/tests/test_project_authz_route_table.py apps/web-server/tests/test_ws_CrossOrg_authz.py apps/web-server/tests/test_scoped_service_tokens.py apps/web-server/tests/test_mcp_remote.py apps/web-server/tests/test_authz_releases_session_1963.py -v
python3 scripts/cq_ratchet.py
```

## Rollback

Revert the branch commits or set `APP_LEGACY_SHARED_TOKEN_FULL_ACCESS=true` if a legacy caller still relies on `APP_API_TOKEN`; no destructive schema migration is performed.
