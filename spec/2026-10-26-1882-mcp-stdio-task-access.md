---
status: approved
issue: 1882
intent: intent/2026-10-26-1882-mcp-stdio-task-access.md
---

# Spec: per-task access check on the mcp-stdio write proxies (#1882)

## Design

`acw_` keys are bound to a user and an organisation (intent, "Facts"), so the
stdio proxy must give the same authz answer as the REST route for that key.

1. **New dependency in `apps/web-server/server/mcp_stdio/auth.py`.**
   `require_acw_access(scope, minimum_role="member")` returns a FastAPI
   dependency that:
   - resolves the key with the existing `require_acw_scope(scope)` (401/403
     unchanged);
   - builds the caller principal the way `TokenAuthMiddleware` does: an
     `AuthenticatedKey` through `server.auth._acw_principal(key,
     scoped_service_tokens_enabled=False)`, the `_LegacyAdminKey` as
     `{"id": "default", "role": "user", "is_service": True}`
     (`server/auth.py:349-355`);
   - reads the target project from the request: the `task_id` path parameter
     (part before `:`, as `TaskAccessChecker` does) or, failing that, the
     `project_id` query parameter (create-and-run);
   - calls the existing `routes.project_authz.authorize_project_for_user(
     principal, project_id, db, minimum_role)` with `db` from `get_db`, so
     the rule, the 404/403 mapping, the dev-mode bypass and the
     `authz.denied` audit row are all reused;
   - returns the key, so handlers keep passing it to `_audit_mcp_write`.
2. **`router.py`.** Every write proxy that targets an existing task or project
   swaps `require_acw_scope(X)` for `require_acw_access(X)`: create-and-run,
   start, stop, recover, approve-plan (`task:write`) and create-pr, merge
   (`task:merge`). All REST counterparts use role `member`, so one default
   covers them. Project create (`project:write`) has no target to check and
   is unchanged. The module docstring states the rule.
3. **apply-correction.** #1881 adds that proxy to the same file. If it merges
   before this PR, it gets the same dependency here; otherwise the PR notes it
   for #1881.

`request.state.user` is not touched, so the delegated handlers see exactly
what they see today.

## Alternatives rejected

- **Treat acw keys as global operator keys (no change).** Contradicted by the
  model (non-null `user_id`/`org_id`), the mint route's membership check and
  the REST surface, which already enforces `require_task_access` on the same
  key.
- **Also confine a key to its own `org_id`.** Stricter than REST, which checks
  the key owner's memberships, not the key's org. That is a new rule; it
  belongs in its own issue if wanted.
- **Set `request.state.user` and call `require_task_access(...)` directly.**
  Reuses one more line but changes what delegated handlers read from
  `request.state.user`; the direct `authorize_project_for_user` call has no
  side effect.
- **Gate the read proxies with `viewer` too.** Same gap, but out of #1882's
  scope; `proxy_list_projects` deliberately returns all projects. Noted as a
  follow-up.

## Risks

- A stdio MCP user whose key belongs to an org that does not own the project
  now gets 403 where it used to work. That is the fix; the error tells them.
- Legacy-token installs: service principal → allowed, unchanged.
- Auth-disabled dev mode: `authorize_project_for_user` returns early, unchanged.
- A task id without `:` is taken whole as the project id, as on REST.

## Verification

- New test module `tests/test_mcp_stdio_task_access.py`: for each of the seven
  write proxies, a key whose user is not a member of the project's org gets
  403 and the delegated handler is not called; the owner's key (member) gets
  200 and the handler runs; an unknown project gets 404. The legacy token
  passes.
- Existing `tests/test_mcp_stdio_*.py` and `tests/test_mcp_task_control.py`
  stay green. ruff + `scripts/ratchet_lint.py` clean on touched files.
