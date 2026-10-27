---
status: approved
issue: 1915
spec: spec/2026-10-27-1915-audit-org-gates.md
---

# Plan: access-review and audit-export org gates

Decisions (from the spec):
- The access review streams `membership.org_id` and takes only `?org_id`.
- The audit export needs admin in `org_id`. Without `org_id`, only a service
  principal or auth-disabled dev may export.
- No role claim is trusted.

## Steps

1. `apps/web-server/server/routes/access_review.py:59-77`: remove the `org`
   Query param and use `membership.org_id` for `_stream_review` and the
   filename. Drop the `# noqa: ARG001` on `membership`, since it is now
   used. In `docs/docs/concepts/audit-anchor.md`, change `?org=` to
   `?org_id=`. Add the access-review cases to `tests/test_audit_org_gates.py`.
   → verify: `apps/backend/.venv/bin/python -m pytest tests/test_audit_org_gates.py tests/audit -q`
   Traps: `Query` may become unused in this file, so let ruff tell you. Use
   absolute imports for anything new (TID252). Never `git add -A`.
2. `apps/web-server/server/routes/audit.py:174-213` `export_audit_logs`: add
   the gate as in the spec. Import `is_service_principal` and `_auth_disabled`
   from `server.routes.project_authz` and `_role_level` from
   `server.routes.organizations`, as absolute imports. Add the export cases to
   the same test file. The test app sets `request.state.user` with a tiny
   middleware and overrides `get_db`/`get_current_user` on the module (see
   `tests/test_api_key_autonomy_ceiling.py`). Patch
   `server.routes.project_authz._auth_disabled` to return False.
   → verify: the same pytest command, plus the cq ratchets (ruff and mypy) and
   `scripts/gen_autonomy_matrix.py --check` (regenerate it if imports change).

## Tests

As above.

## Rollback

Revert the PR.
