---
status: approved
issue: 1908
spec: spec/2026-10-27-1908-api-key-scope-cap.md
---

# Plan: cap acw_ key scopes by org role, and stop trusting a JWT role claim

Approved decisions, carried over from the spec:
- A caller whose org role is below `member` (viewer, or an unknown role at
  level -1) may request only `mcp:read` (`MCP_READ_SCOPE` from
  `server.mcp_remote.auth`). Anything else gets a 403. `scopes=None` and
  `[]` stay allowed. Member and above are unchanged. No new roles or
  scope constants.
- `is_service_principal` returns `bool(user.get("is_service"))` only. The
  auth-disabled principals that relied on `role == "admin"` get
  `"is_service": True` explicitly. Keep their `role` key.
- PF/TF are untouched (no project RBAC there; Factory#312). Remote MCP
  cross-org authz is #1910 and out of scope. Existing keys are not revoked.

## Steps

1. `apps/web-server/server/routes/api_keys.py` `create_api_key`: after the
   API-key-cannot-mint check (around line 170) and before the ceiling check,
   add the scope cap:
   `if _role_level(membership.role) < _role_level("member") and set(body.scopes or ()) - {MCP_READ_SCOPE}:`
   raise 403 with detail "Scopes beyond 'mcp:read' require the 'member' org role".
   Add a one-line why comment citing Factory#312 / #1908.
   Import: `from ..mcp_remote.auth import MCP_READ_SCOPE`.
   Tests in `tests/test_api_key_autonomy_ceiling.py`: add a `viewer-u` user
   with `OrgMember(role="viewer")` in the `client` fixture (lines 77-83). Add:
   - viewer + `scopes=["mcp:write"]` gives 403 (fails on dev)
   - viewer + `scopes=["mcp:read", "task:write"]` gives 403
   - viewer + `scopes=["mcp:read"]` gives 201
   - member + `scopes=["mcp:write"]` gives 201
   → verify: `apps/backend/.venv/bin/python -m pytest tests/test_api_key_autonomy_ceiling.py -q`
   Traps: if the import cycles at module load, import lazily inside the
   function and record that as a deviation. Never `git add -A`, because
   `apps/backend/.venv` is a symlink.

2. `apps/web-server/server/routes/project_authz.py:48-57` `is_service_principal`:
   drop the role clause and update the docstring ("legacy API_TOKEN identity
   and the auth-disabled dev principal, both marked `is_service`; a role
   claim never counts"). Same file, line 225 fallback dict: add
   `"is_service": True`. `apps/web-server/server/auth.py:283-287` DISABLE_AUTH
   principal: add `"is_service": True`.
   Tests in `tests/test_project_authz.py`: line 44 asserts False. Line 59
   becomes `{"role": "admin", "id": "default", "is_service": True}`. Add a
   test that `check_project_access({"id": "u1", "role": "admin"}, {"org_id": "o"}, None, "viewer")`
   raises 403 (fails on dev).
   → verify: `apps/backend/.venv/bin/python -m pytest tests/test_project_authz.py tests/rmux -q`
   and `git grep -n '"role": "admin"' apps/web-server/server`. Every
   principal dict it lists must also carry `is_service`.
   Traps: same `git add` rule.

## Tests

`apps/backend/.venv/bin/python -m pytest tests/test_api_key_autonomy_ceiling.py tests/test_project_authz.py tests/rmux -q`
passes. `ruff check` and `ruff format --check` pass on the touched files. Pre-commit runs the rest.

## Rollback

Revert the PR. No migration, no data change.

## Deviations

- Step 1: the import is absolute, `from server.mcp_remote.auth import MCP_READ_SCOPE`, not
  relative. The cq-ratchet strict baseline flags new relative parent imports (TID252).
