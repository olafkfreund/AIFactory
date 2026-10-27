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
- The generated autonomy matrix (`docs/docs/compliance/autonomy-matrix.md` and `.json`) was
  regenerated with `scripts/gen_autonomy_matrix.py`. The new `server.routes.organizations`
  import raises the `pr_review_service` reach count from 277 to 280. Nothing else changed.

## Review changes (lead's Opus review of PR #1914)

Once keys work, REST and stdio key access ignores the key's scopes and its org. These steps
close that before merge. Decisions:
- REST needs the explicit scope `api` (`API_SCOPE`, mirroring PFactory). With the existing cap
  (below member, only `mcp:read`), `api` requires member or above. The #1911 mint cap already
  refuses it for viewers, so the mapping does not change.
- A key whose effective (capped) scopes are empty fails authentication everywhere.
- A key principal (`api_key_id` set) is bound to `key.org_id` for project authz, list
  filters, project-create ownership and org-role routes.
- A key whose org is missing or has `deleted_at` set fails authentication.
- Minting stores `expires_at` as naive UTC. Verified locally that asyncpg rejects an aware
  datetime in the naive `DateTime` column with "can't subtract offset-naive and offset-aware".

2. `apps/web-server/server/mcp_remote/auth.py`:
   - Add `API_SCOPE = "api"` next to `MCP_READ_SCOPE` / `MCP_WRITE_SCOPE`.
   - In `_lookup_by_digest`, after the owner check, load `session.get(Organization, row.org_id)`.
     A missing org or `deleted_at` set raises `MCPAuthError("API key organization is deleted")`.
   - After the role cap, empty scopes raise
     `MCPAuthError("API key has no scopes for its owner's current role")`.
   - Update the `authenticate` docstring bullets.
   - Update `tests/test_acw_key_lookup.py`:
     - the removed-member test now expects that error;
     - add a deleted org (`deleted_at` set) failing;
     - add stored scopes `""` failing;
     - add a viewer with only `api` failing (capped to empty).
   → verify: `pytest tests/test_acw_key_lookup.py -q`

3. `apps/web-server/server/auth.py` Strategy 3 (around lines 362-373): after
   `_authenticate_acw`, if `API_SCOPE not in key.scopes`, do not authenticate. Fall through
   to the same 401 as an invalid key.
   Add real-DB REST middleware tests in a new `tests/test_acw_rest_scope.py`. Use
   `TokenAuthMiddleware` on a tiny app, monkeypatch `server.mcp_remote.auth.get_db` to yield
   a SQLite session, and use the `no_disable_auth` pattern from `tests/test_auth_acw_main_api.py`.
   Cases:
   - an `api`-scoped member key gets 200;
   - an `mcp:read`-only key gets 401;
   - an empty-scope key gets 401;
   - a deleted-org key gets 401;
   - an other-org key gets 403 on a project route guarded by `require_project_access()`
     (monkeypatch `server.project_registry.load_projects` so a project belongs to org-2,
     where the owner is also a member).
   → verify: `pytest tests/test_acw_rest_scope.py tests/test_auth_acw_main_api.py -q`

4. Bind key principals to `key.org_id`:
   - `apps/web-server/server/routes/project_authz.py`:
     - `check_project_access`: after the project-org check, if `user.get("api_key_id")` and
       `user.get("org_id") != org_id`, raise 403 "This API key is scoped to another
       organization" (before the membership check).
     - `accessible_org_ids`: for a key principal, intersect the result with `{user["org_id"]}`.
     - `resolve_owner_org_id`: for a key principal, return `user["org_id"]`.
   - `apps/web-server/server/routes/organizations.py` `OrgRoleChecker.__call__`: add
     `request: Request` and the same 403 when a key principal's org is not the path org.
   Tests:
   - pure tests in `tests/test_project_authz.py`;
   - a stdio test in `tests/test_mcp_stdio_task_access.py`: a member of both orgs using an
     org-1 key gets 403 on an org-2 project task write and on a read, and org-2 projects are
     filtered out of list results.
   → verify: `pytest tests/test_project_authz.py tests/test_mcp_stdio_*.py tests/rmux -q`
   Not covered: `git_credentials._verify_org_membership` and `access_review` take a `User`,
   not the principal, so they stay unbound. That goes in the follow-up issue; key creation
   with a key is already refused.

5. `apps/web-server/server/routes/api_keys.py` around line 193: store
   `(datetime.now(UTC) + timedelta(days=...)).replace(tzinfo=None)`, with a one-line why
   comment. Add `tests/postgres/test_api_key_expiry_pg.py`, marked `postgres` and `slow`
   like its siblings and using the `test_postgres_url` fixture. It mints with
   `expires_in_days=1` through the api_keys router on PG and gets 201, and
   `_lookup_by_digest` accepts the key. With `expires_at` set to the past, the lookup
   rejects it.
   → verify: `TEST_POSTGRES_URL=postgresql+asyncpg://pgtest@127.0.0.1:55432/t pytest tests/postgres/test_api_key_expiry_pg.py -m postgres -q`
   (a local throwaway PG is running on 55432).

Traps for every step:
- Use absolute `server.` imports (TID252).
- Never `git add -A`.
- Run the cq ratchets and `scripts/gen_autonomy_matrix.py --check`, and regenerate if an
  import changed.
- Step 5: the PG test creates its tables in its own schema (`apikey_expiry_pg`, set through
  `search_path`) instead of calling `drop_all` on public. The sibling PG tests read the
  alembic-migrated public schema.
- Step 3: the REST `api` check lives in `mcp_remote/auth.py::authenticate_rest`. Strategy 3
  imports that instead of `authenticate`, which keeps `TokenAuthMiddleware.dispatch` at its
  base ruff counts (PLR0912, PLC0415, I001). `resolve_owner_org_id` returns `str(...)`
  (mypy no-any-return).
