---
status: approved
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

## Deviations

- Step 1: `_lookup_by_digest` checked `row.is_active`, but `ApiKey` has no such column. Every
  real key raised AttributeError, so acw_ keys never authenticated on dev: REST returned 401,
  stdio and remote MCP returned 500. The lead decided to replace that dead check with an owner
  check: a missing owner or `User.is_active` False raises "API key owner is deactivated". This
  matches PFactory's REST path and covers SCIM soft-deprovisioning.
  Before the change I confirmed that revoke hard-deletes the row and that `api_keys` never had
  an `is_active` or soft-delete column.
  Behaviour change: acw_ keys start authenticating on AIF REST, stdio and remote MCP.
- The test file uses real-DB tests for revoked, expired, deactivated-owner, demoted-owner and
  removed-member keys, as the lead asked.
