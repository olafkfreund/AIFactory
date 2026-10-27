---
status: draft
issue: 1913
spec: spec/2026-10-27-1913-acw-auth-expiry-role.md
---

# Plan: acw_ auth enforces expiry and the owner's current org role

Decisions (from the spec):
- Expiry and the role cap are enforced in `_lookup_by_digest` only.
- The mapping is: below member, only `mcp:read`; no membership, no scopes;
  member and above, stored scopes. `user_id` None is unchanged.
- No schema change and no revocation.

## Steps

1. `apps/web-server/server/mcp_remote/auth.py:114-136`: add the expiry check
   and the role cap (the helper `_cap_by_owner_role`), plus imports
   `datetime, UTC`, `OrgMember` and `server.routes.organizations._role_level`.
   Update the `authenticate` docstring to list expired keys. Add
   `tests/test_acw_key_lookup.py`.
   → verify: `apps/backend/.venv/bin/python -m pytest tests/test_acw_key_lookup.py tests/test_mcp_remote.py tests/test_auth_acw_main_api.py -q`,
   then the cq_ratchet ruff command from `.github/workflows/cq-ratchet.yml`.
   Traps: use absolute imports (TID252). Never `git add -A` (the .venv symlink).

## Tests

Above, plus the stdio MCP tests (`tests/ -k mcp_stdio`).

## Rollback

Revert the PR.
