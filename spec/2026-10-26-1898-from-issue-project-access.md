---
status: draft
issue: 1898
intent: intent/2026-10-26-1898-from-issue-project-access.md
---

# Spec: project access check on from-issue intake

## Design

`create_from_issue` gains `db: AsyncSession = Depends(get_db)`. Its first statement, before the project is resolved, is:

`await authorize_project_for_user(raw_request.state.user, request.project_id, db, "member")`

That is the function `ProjectAccessChecker._authorize` (behind `require_project_access`) uses. It returns at once for auth-disabled deployments and for service principals (the legacy token, used by the poller). Otherwise it raises 401, 404, 403 or the role-gate 403, and records denials as audit events. The check runs only when `raw_request` is present. Direct in-process callers pass none, which is the convention `tasks.create_task` already follows (#1466); over HTTP FastAPI always injects it.

## Alternatives rejected

- **`Depends(require_project_access("member"))` as-is:** the checker reads `project_id` as a path or query parameter. On this route that would be a required query parameter, so every existing caller (poller, GH Action) would get a 422. That breaks intake and gives no security.
- **Move `project_id` to the query string:** changes the public intake contract for every caller.
- **A body-reading dependency class:** more code than one call to the shared function, with the same rule.

## Risks

- A deployment whose poller uses an `acw_` key without membership of the target org would now get 403, which is correct. The default deployment uses the legacy token.
- Unknown `project_id` from a non-service caller now returns 404 from authz rather than the route's own 404, which is the same status.

## Verification

New tests in `tests/test_from_issue_project_access.py`. They seed in-memory SQLite (pattern from `tests/test_project_authz_db.py`), run the real authz with enforcement on, and send requests through a FastAPI app that mounts `from_issue.router`:

- an `acw_` key principal from another org → 403, no spec written;
- an `acw_` key principal that is `member` of the project's org → 200, spec written;
- the legacy service principal → 200;
- the poller's own `_route_low_medium`, with its HTTP POST sent to the test app → task created (no 422 or 403).

The existing `tests/test_from_issue_route.py` stays green.
