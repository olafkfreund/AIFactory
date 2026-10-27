---
status: approved
issue: 1913
intent: intent/2026-10-27-1913-acw-auth-expiry-role.md
---

# Spec: acw_ auth enforces expiry and the owner's current org role

## Design

All in `apps/web-server/server/mcp_remote/auth.py::_lookup_by_digest`, the
single choke point that `authenticate()` uses for REST, stdio and remote:

1. After the `is_active` check, if `row.expires_at` is set and is `<= now`, raise
   `MCPAuthError("API key has expired")`. A naive datetime (SQLite) is treated as
   UTC, matching TFactory's lookup.
2. If `row.user_id` is set, look up `OrgMember.role` for
   `(row.org_id, row.user_id)`. No row gives `frozenset()`. If
   `_role_level(role) < _role_level("member")`, intersect with `{MCP_READ_SCOPE}`.
   Otherwise keep the stored scopes. `_role_level` comes from
   `server.routes.organizations` (absolute import, TID252).

This is the same mapping as #1911's mint-time cap. When both have landed, the
mint check could reuse this helper, but that is out of scope here.

## Alternatives rejected

- Revoke offending keys with a migration. That only fixes the past: demotion
  later would reopen the gap, and it destroys rows.
- Check the role in each remote tool. That duplicates the logic per path, and
  REST and stdio would still see the stored scopes.

## Risks

- A demoted user's key silently loses write scopes and now gets a 403 on writes.
  That is intended.
- Expired keys now get a 401 on REST too. They should already have been failing.

## Verification

New `tests/test_acw_key_lookup.py`, using a SQLite DB and `_lookup_by_digest`
directly. On dev, the expired, viewer, demoted and no-membership tests fail:
- an expired key raises;
- a viewer's stored `mcp:write` key resolves to `{mcp:read}`;
- a removed member's key resolves to no scopes;
- a member's key keeps its scopes;
- a future `expires_at` passes.
Run `tests/test_mcp_remote.py`, `tests/test_auth_acw_main_api.py` and `tests/mcp_stdio*`.
