---
status: approved
issue: 2181
intent: intent/2026-11-20-2181-aif-project-authz.md
---

# Spec: Project-level authorization (`require_project_role`) across all surfaces

## Design

### 1. Shared contract: `require_project_role(principal, project_id, min_role)`

Implemented in `apps/web-server/server/routes/project_authz.py` (with `ROLE_LEVELS = {"viewer": 0, "member": 1, "admin": 2, "owner": 3}` in `server/utils/roles.py`):

- **Canonical resolution**: Look up `project_id` via `await aload_projects()` (`ProjectStore`). If `project_id` is unknown or not in the active registry, raise `HTTPException(404, "Project not found")`.
- **Team grant resolution (`team_grant_role`)**:
  - Single-user / local mode (`settings.DISABLE_AUTH` or `seed_tenant_defaults` in `server/database/engine.py:266-368`): the default org has one `is_default=True` team (`team-default`), and `DEFAULT_USER_ID` (`"default"`) is seeded into `TeamMembership` as `owner`. Existing `OrgMember` rows are backfilled into their org's default team on startup and OIDC/SAML provisioning (`server/oidc/provisioning.py:82-161`).
  - If the principal carries a key `org_id` and `project.org_id != principal["org_id"]`, the grant is `None`.
  - For a user or user-bound `acw_` key (`user_id` set): query the highest role from `TeamMembership` where `user_id == principal["id"]` and `team_id == project.team_id` (falling back to the org's `is_default=True` team when `project.team_id` is null, and `OrgMember` role on that default team if not yet backfilled).
  - For an ownerless scoped service key (`acw_` with `user_id is None`, `scoped_service=True`): grant is resolved from the key's bound `org_id` default team (as `admin` ceiling before scope intersection) only for projects in `key.org_id`; projects outside `key.org_id` resolve to `None`.
- **Scope ceiling (`scope_ceiling(scopes)`)**:
  - User sessions (OIDC/SAML/JWT login where `"scopes"` is absent or `None`): ceiling is `"owner"` (team grant alone governs).
  - Token principals (`"scopes"` present on principal):
    - Read scopes (`mcp:read`, `project:read`, `task:read`) -> `"viewer"`
    - Write scopes (`project:write`, `task:write`, `plan:write`, `mcp:write`, `api`) -> `"member"`
    - Merge / admin scopes (`task:merge`, `project:admin`, `admin`) -> `"admin"`
    - Highest matched scope wins; unrecognized/empty scopes -> `None`.
- **Effective role & HTTP status**:
  - If `team_grant_role is None` or `scope_ceiling is None`: the project is not visible to the principal -> raise **`404 Project not found`** (identical to unknown project; no audit oracle).
  - `effective_role = min_role_by_level(team_grant_role, scope_ceiling)`.
  - If `ROLE_LEVELS[effective_role] < ROLE_LEVELS[min_role]`: project is visible (`>= viewer`), so emit chained denial audit (`_audit_authz_denial` after `await db.rollback()`) and raise **`403 Forbidden`**.
  - On success: `await db.rollback()` to release the read transaction (#1963) and return `ProjectAccess(project_id, org_id, team_id, role=effective_role)`.
- **List filtering (`accessible_project_ids(request, db, minimum_role="viewer")`)**:
  - Evaluates effective role across `await aload_projects()` and returns only project IDs where `effective_role >= minimum_role`.

### 2. Legacy shared token gate (`LEGACY_SHARED_TOKEN_FULL_ACCESS`)

- Add `LEGACY_SHARED_TOKEN_FULL_ACCESS: bool = True` to `Settings` (`server/config.py:165`, env `APP_LEGACY_SHARED_TOKEN_FULL_ACCESS`).
- In `server/auth.py` (`_resolve_principal:400`, `require_auth:466`, `authenticate_websocket:514`) and `server/mcp_stdio/auth.py:122,158`:
  - When authenticated via `settings.API_TOKEN` (`APP_API_TOKEN`), return principal `{"id": "default", "role": "user", "is_legacy_shared_token": True, "is_service": False}` and log a deprecation warning (`logger.warning("Deprecated APP_API_TOKEN used; migrate to scoped acw_ key")`).
  - When authenticated via an `acw_` key (`_acw_principal`, `_ws_acw_principal`, `acw_principal`), ALWAYS attach `"scopes": sorted(key.scopes)`, `"org_id": key.org_id`, `"scoped_service": key.user_id is None`, and `"is_service": False`. Scoped keys never bypass `require_project_role`.
  - `require_project_role` grants `"owner"` to `is_legacy_shared_token` ONLY when `settings.LEGACY_SHARED_TOKEN_FULL_ACCESS` is `True` (or `settings.DISABLE_AUTH` is `True`). When `False`, `APP_API_TOKEN` has no bypass.

### 3. Complete project-data surface inventory

| Surface / Path | Code Location | How Owning Project Is Resolved | Required Role |
| :--- | :--- | :--- | :--- |
| `GET /api/projects` | `routes/projects.py:457` | Filter `aload_projects()` via `accessible_project_ids` | `viewer` (filter) |
| `GET /api/projects/by-slug/{slug}` | `routes/projects.py:76` | Match visible projects by slug/name -> `project_id` | `viewer` |
| `POST /api/projects/scan`, `POST /api/projects` | `routes/projects.py:532,671` | Caller target org default team (`resolve_owner_org_id`) | `member` |
| `GET /api/projects/{project_id}` (`version`, `worktrees`, `tasks`, `tasks/{spec_id}/logs`) | `routes/projects.py:814,954,1151,1315,1537` | Path `project_id` | `viewer` |
| `PUT /api/projects/{project_id}`, `POST .../initialize`, `POST .../tasks`, `POST .../tasks/(un)archive`, `POST .../logs/(un)watch` | `routes/projects.py:830,906,1375,1507,1570,1646` | Path `project_id` | `member` (`viewer` for log watch) |
| `DELETE /api/projects/{project_id}`, `PATCH /api/projects/{project_id}/settings` | `routes/projects.py:878,1049` | Path `project_id` | `admin` |
| `GET/POST /api/projects/{projectId}/changelog/*` (reads/previews vs `generate`, `save`, `images`) | `routes/changelog.py:143-885` | Path `projectId` (router/endpoint dep) | `viewer` (reads) / `member` (writes) |
| `GET/POST/PATCH/DELETE /api/projects/{projectId}/insights/*` | `routes/changelog.py:892-1213` | Path `projectId` | `viewer` (`GET`) / `member` (mutations) |
| `DELETE /api/projects/{projectId}/files/insights` | `routes/files.py:998` | Path `projectId` | `member` |
| `GET /api/projects/{projectId}/(context\|memory/*\|claude-auth)` | `routes/context.py:88,247,286,346,846` | Path `projectId` | `viewer` |
| `POST /api/projects/{projectId}/(context/refresh\|claude-setup)` | `routes/context.py:194,852` | Path `projectId` | `member` |
| `GET/PATCH /api/projects/{projectId}/env` | `routes/context.py:424,579` | Path `projectId` (contains secrets/keys) | `admin` |
| `GET/POST /api/projects/{projectId}/github/*`, `.../git/(squash\|worktree)`, `.../releases/*` | `routes/github.py:904-2376`, `git.py:1123-1570`, `project_github.py:14` | Path `projectId` in `project_github_token` (always run even when `multi_installation()` is false) | `viewer` (`GET`) / `member` (`POST`) / `admin` (`merge`, `releases` create) |
| `GET/POST /api/projects/{project_id}/auto-fix/*` | `routes/auto_fix.py:70-158` | Path `project_id` | `viewer` / `member` / `admin` (`/config`) |
| `GET /api/projects/{project_id}/mcp-status` | `routes/mcp.py:77` | Path `project_id` | `viewer` |
| `GET /api/files/{project_id}/(list\|read\|search\|diff)` | `routes/files.py:572,627,749,887` | Path `project_id` | `viewer` |
| `PUT /api/files/{project_id}/write`, `DELETE /api/files/{project_id}/delete` | `routes/files.py:677,711` | Path `project_id` | `member` (`write`) / `admin` (`delete`) |
| `GET /api/files/(read\|serve\|list\|discover)` | `routes/files.py:259,349,404,480` | `project_id_for_path(path\|root)` (filter invisible project dirs in `list`/`discover`) | `viewer` (on owning project; `member` for non-project host browse) |
| `GET /api/git/(branches\|current-branch\|main-branch\|status)`, `POST /api/git/init` | `routes/git.py:78,89,99,116,150` | `project_id_for_path(path)` if inside registered project | `viewer` (`GET`) / `member` (`init`) |
| `GET /api/github/(detect-repo\|fork-info)`, `GET /api/github/auto-detect`, `POST /api/github/(persist-token\|remote)` | `routes/github.py:507,767,829,878,2430` | Query/body `projectId` or `project_id_for_path(path\|projectPath)` | `viewer` (`detect-repo`, `fork-info`) / `admin` (`auto-detect`, `persist-token`, `remote`) |
| `GET /api/tasks` | `routes/tasks.py:123` | Query `project_id` -> `require_project_role` (404 if invisible); if omitted -> filter by `accessible_project_ids` | `viewer` |
| `POST /api/tasks` | `routes/tasks.py:246` | Body `TaskCreate.project_id` | `member` |
| `GET /api/tasks/running` | `routes/execution.py:301` | Filter running `task_id.partition(":")[0]` by `accessible_project_ids` | `viewer` (filter) |
| `POST /api/tasks/create-and-run`, `POST /api/tasks/from-plan`, `POST /api/tasks/from-issue` | `routes/execution.py:1298,1568`, `from_issue.py:421` | Resolve `resolve_project_id(project_id)` first (404 if unknown), then `require_project_role` | `member` |
| `/api/tasks/{task_id}/*` (`tasks.py`, `execution.py`, `tasks_views.py`, `tasks_logs.py`, `tasks_usage.py`, `tasks_clarifications.py`, `tasks_regression.py`, `plan_approval.py`, `pr.py`, `inbox.py`, `worktree_merge.py`) | `routes/project_authz.py:263` (`require_task_access`) | `task_id.partition(":")[0]` (reject bare IDs without `:` with 400) | `viewer` (`GET`) / `member` (writes, `create-pr`) / `admin` (`worktree/merge`) |
| `POST /api/tasks/worktree/(open-in-ide\|open-in-terminal)` | `routes/worktree_tools.py:259,299` | `project_id_for_path(worktreePath)` | `member` |
| `POST/GET/DELETE /api/tasks/{spec_id}/agent-console/*` & `WS .../ws` | `rmux/bridge.py:230,297,348` | Authoritative `state.project_id` from rmux session registry (#322) | `viewer` (`ws`, `detach`) / `member` (`attach`) |
| `GET/POST /api/maintenance/(merger\|stale-tasks)/*` | `routes/merger.py:88,108`, `stale.py:171,182` | Filter scanned projects by `accessible_project_ids` | `viewer` (status/dry-run) / `member` (run/reap) |
| `POST /api/investigations`, `GET/POST /api/investigations/{run_id}/*` | `routes/investigations.py:78-153` | Body `project_id` or `InvestigationStore.get(run_id).project_id` | `viewer` (`GET`) / `member` (`POST`) |
| `/api/terminals/*` & `WS /ws/terminal/{session_id}` | `routes/terminal.py:70-476`, `websockets/terminal.py:310`, `pty/access.py:17` | `project_id_for_path(cwd\|project_path)` or session `project_id` | `viewer` (`GET`) / `member` (create/mutate/WS shell) |
| `WS /ws/tasks/{task_id}/(logs\|progress)`, `WS /ws/(logs\|progress)`, `WS /ws/events` | `websockets/logs.py`, `progress.py`, `events.py`, `task_access.py` | `task_id.split(":", 1)[0]` or event `project_id` filtered by `require_project_role` | `viewer` |
| `/api/mcp-stdio/*` | `mcp_stdio/router.py:76-521`, `mcp_stdio/auth.py:183` | `project_id` query/body, `task_id` prefix, or `InvestigationStore` via `require_acw_access` | `viewer` (`mcp:read`) / `member` (`*:write`) / `admin` (`task:merge`) |
| `/api/mcp-remote/*` (12 MCP tools) | `mcp_remote/tools.py:89-346` | `list_projects` filtered; `project_id` arg or `task_id` prefix via `_authorize_call` | `viewer` (read tools) / `member` (write tools) / `admin` (`merge_pr`) |
| `POST /mcp` (`copilot_mcp`) | `routes/copilot_mcp.py:180,324` | Resolve `project_id_for_path(spec_dir.parent.parent.parent)` after `_resolve_task` | `viewer` (`get_*`, `list_*`) / `member` (`answer_clarification`) |

## Alternatives rejected

- **Middleware regex on `/api/projects/{id}`**: Misses query/body `project_id`, `task_id` prefixes, filesystem paths, WebSockets, and MCP tool calls.
- **Keeping `is_service` bypass for ownerless `acw_` keys**: Violates least privilege; service tokens must be constrained by `team_grant ∩ scope_ceiling`.

## Risks

- **Caller breakage on 403 -> 404 change**: Existing tests expecting `403` when a non-member accesses another org's project by ID must be updated to expect `404` (while keeping `403` when a `viewer` attempts a `member`/`admin` action).
- **Automation using `APP_API_TOKEN`**: Mitigated by defaulting `APP_LEGACY_SHARED_TOKEN_FULL_ACCESS=True` in phase 2 and logging deprecation warnings.

## Verification

- Generated route-table test (`apps/web-server/tests/test_project_authz_route_table.py`) walks REST routes, WebSockets, and MCP tools and fails if any project-scoped route lacks `require_project_role` / `accessible_project_ids`.
- Matrix tests covering: unknown project -> 404; invisible project -> 404; visible project with insufficient role or scope ceiling -> 403; `team_grant ∩ scope_ceiling` combinations; cross-team service token denial; `APP_LEGACY_SHARED_TOKEN_FULL_ACCESS=false`.
