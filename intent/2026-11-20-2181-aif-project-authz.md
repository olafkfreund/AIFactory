---
status: approved
issue: 2181
author: olafkfreund
---

# Intent: Enforce project-level authorization across REST, WebSocket, MCP, and agent endpoints

## Problem

Phase 1 (#2157, epic `olafkfreund/Factory#3857`) introduced the DB-backed project registry, `Team` / `TeamMembership` tables, and `Project.team_id`, but project authorization in `apps/web-server/server/routes/project_authz.py` still has three critical gaps:

1. **Blanket service-principal bypass**: `is_service_principal(user)` (`project_authz.py:50-60`) short-circuits `check_project_access` (`:85`) and `authorize_project_for_user` (`:141`). Because `server/auth.py:241` sets `"is_service": key.user_id is None` on ownerless `acw_` keys, both the legacy `APP_API_TOKEN` and scoped `acw_` service tokens bypass project checks completely.
2. **Org-only checks and existence leaks**: `check_project_access` checks `OrgMember` instead of intersecting the principal's project team grant with the token's scope ceiling. Single-project routes return `403` for cross-org projects vs `404` for unknown IDs, and `GET /api/tasks?project_id=<other>` returns `200 []` vs `404` for unknown IDs, leaking project existence.
3. **Unguarded routes and indirect resolvers**: Sub-routers (`changelog.py`, `context.py`, `files.py` insights, `mcp.py` `/{project_id}/mcp-status`), `project_github_token` when `github_app.multi_installation()` is false (`github.py`, `git.py`), path/body routes (`files.py` `/read`, `/serve`, `/list`, `/discover`; `git.py` `/branches`, `/status`, `/init`; `github.py` `/auto-detect`, `/persist-token`, `/detect-repo`, `/remote`, `/fork-info`; `worktree_tools.py` `/open-in-ide`, `/open-in-terminal`), task endpoints (`POST /api/tasks`, `GET /api/tasks/running`, `POST /api/tasks/from-plan` before `resolve_project_id`), `stale.py` maintenance, and `copilot_mcp.py` bare task lookups lack `require_project_role`.

## Proposed outcome

- **Single enforcement contract (`require_project_role(principal, project_id, min_role)`)** shared across AIFactory#2181 (reference), PFactory#1099, and TFactory#1650:
  - Roles: `viewer < member < admin < owner` (`ROLE_LEVELS`).
  - Effective role = `min(team_grant_role(principal, project), scope_ceiling(principal.scopes))`. User sessions (OIDC/portal JWT) use team grants only.
  - Scope ceiling: read (`mcp:read`, `*:read`) -> `viewer`; write (`project:write`, `task:write`, `plan:write`, `mcp:write`) -> `member`; merge/admin (`task:merge`, `*:admin`, `admin`) -> `admin`.
  - Uniform error semantics: raises **404** when the project is unknown OR not visible to the principal (`< viewer`), and **403** when visible (`>= viewer`) but effective role `< min_role`.
  - List endpoints return only projects where the principal has at least `viewer`.
- **Legacy shared token gate**: `APP_API_TOKEN` retains full access only while `LEGACY_SHARED_TOKEN_FULL_ACCESS` (`APP_LEGACY_SHARED_TOKEN_FULL_ACCESS`, default `True` in phase 2) is enabled, logging a deprecation warning on each use. Scoped `acw_` service keys never bypass. Single-user/local mode seeds one default team with the local `default` user as `owner`.
- **Generated route-table authz test**: walks REST, WebSocket, and MCP tools and fails CI if any project-scoped entry lacks `require_project_role`.

## Affected users and systems

- `apps/web-server/server/` (`config.py`, `auth.py`, `database/`, `routes/`, `websockets/`, `pty/`, `rmux/`, `mcp_stdio/`, `mcp_remote/`).
- Mirrors to PFactory (`#1099`) and TFactory (`#1650`).
- Running cluster and `parr-run` workflows using `APP_API_TOKEN` or `acw_` keys.

## Constraints

- Must not break running cluster or `parr-run` in phase 2 (`APP_LEGACY_SHARED_TOKEN_FULL_ACCESS=true` default).
- Must release DB transaction (`await db.rollback()`) before/after authz checks and denial audits (#1919, #1963).
- Must keep vendored `apps/web-server/factory_common/` byte-identical and pass `scripts/cq_ratchet.py`.

## Open questions

1. **Cross-team grants in Phase 2**: Should `team_grant_role` resolve strictly from `TeamMembership` on `Project.team_id` (plus org default-team backfill for existing `OrgMember` rows), or also introduce a `project_team_grants(project_id, team_id, role)` table now for multi-team access to a single project?
2. **Legacy `api`-only user keys**: For existing user-owned `acw_` keys created with `scopes=["api"]` (`mcp_remote/auth.py:37`), should `"api"` map to a `member` scope ceiling or require explicit `*:read` / `*:write` / `task:merge` scopes?

## Decisions (coordinator, 2026-11-20, under the owner's standing delegation)

1. Phase 2 resolves `team_grant_role` strictly from `TeamMembership` on `Project.team_id`, plus the org default-team backfill. No `project_team_grants` table yet: cross-team grants belong to phase 5 (team management UX).
2. A user-owned `acw_` key with only the legacy `api` scope caps at `member`.
