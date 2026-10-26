---
status: draft
issue: 1898
spec: spec/2026-10-26-1898-from-issue-project-access.md
---

# Plan: project access check on from-issue intake

Decisions (from the spec):

- The rule is `authorize_project_for_user(user, request.project_id, db, "member")` (`routes/project_authz.py:115`), called first in `create_from_issue`.
- `project_id` stays in the body. No query parameter, so `Depends(require_project_access)` is not used directly: it would 422 every caller.
- The service principal (legacy token, used by the poller) and auth-disabled deployments pass. Direct in-process calls with `raw_request=None` skip the check (#1466 convention).
- The implementer is Claude: two files, under the handoff threshold.

## Steps

1. `apps/web-server/server/routes/from_issue.py`: import `Depends`, `AsyncSession`, `get_db` (`..database.engine`) and `authorize_project_for_user` (`.project_authz`); add `db: AsyncSession = Depends(get_db)` to `create_from_issue` (line ~300) and the check as its first statement when `raw_request is not None`.
   → verify by `pytest tests/test_from_issue_route.py -q`
   Traps: the module has `from __future__ import annotations`; plain `Depends` defaults resolve. Keep the route's branch budget.
2. `tests/test_from_issue_project_access.py`: the four cases in the spec. Enforcement is pinned on with `_auth_disabled`, because CI runs with DISABLE_AUTH=true. The DB is seeded SQLite overriding `get_db`. The poller case calls `intake_poller._route_low_medium` with `_post_json` routed through `TestClient`.
   → verify by `pytest tests/test_from_issue_project_access.py -q`

## Tests

`pytest tests/test_from_issue_project_access.py tests/test_from_issue_route.py tests/test_project_authz_db.py -q`; `ruff check` and `ruff format --check` on the touched files.

## Rollback

Revert the PR; the route returns to authentication-only.
