---
status: draft
issue: 1908
intent: intent/2026-10-27-1908-api-key-scope-cap.md
---

# Spec: cap acw_ key scopes by org role, and stop trusting a JWT role claim

## Design

1. **Scope cap at mint** (`apps/web-server/server/routes/api_keys.py::create_api_key`).
   After the membership lookup:
   if `_role_level(membership.role) < _role_level("member")` and the requested
   scopes contain anything other than `MCP_READ_SCOPE` (imported from
   `server.mcp_remote.auth`), return 403 with "Scopes beyond 'mcp:read'
   require the 'member' org role". Unknown roles (level -1) count as below
   member. `scopes=None` and `[]` stay allowed. This is the only place that
   writes `ApiKey.scopes`, so it is the one choke point.
2. **Service principal is `is_service` only**
   (`routes/project_authz.py::is_service_principal`): drop
   `or user.get("role") == "admin"`. Callers that relied on the role for dev
   mode get the marker explicitly:
   - `auth.py` TokenAuthMiddleware DISABLE_AUTH principal adds `"is_service": True`.
   - `ProjectAccessChecker._authorize` auth-disabled fallback dict adds `"is_service": True`.
   `rmux/bridge.py` and `mcp_stdio/auth.py` already set `is_service`.
   `services/originator.py` already treats `id == "default"` as a service.

## Alternatives rejected

- Enforce scopes in remote MCP per call against the owner's role. That is the
  right fix for #1910 (cross-org), but it is a larger change. The mint-time
  cap closes the viewer escalation now.
- Strip `role` from the JWT. Other code may read the claim, and this would
  change the token shape. Not needed once the authz check ignores the claim.
- Apply the same cap in PFactory/TFactory. Verified not an escalation there:
  they have no project-level RBAC, so a viewer's JWT can already start tasks
  over REST. Their RBAC belongs to Factory#312.

## Risks

- A viewer automation that minted write keys now gets a 403. That is intended.
- A test or dev path that relied on `role == "admin"` meaning service would
  now be denied. The grep on dev found only `tests/test_project_authz.py`
  lines 44 and 59, which encode the old rule and get updated.

## Verification

- New tests in `tests/test_api_key_autonomy_ceiling.py` (it already has the
  api_keys router fixture): a viewer asking for `mcp:write` gets 403, a viewer
  asking for `mcp:read` gets 201, a member asking for `mcp:write` gets 201.
  The viewer/`mcp:write` test fails on dev.
- `tests/test_project_authz.py`: `is_service_principal({"role": "admin"})` is
  False (fails on dev), and `check_project_access` with a role-admin non-service
  user and no membership raises 403.
- Targeted pytest on those files plus `tests/rmux`, and ruff.
