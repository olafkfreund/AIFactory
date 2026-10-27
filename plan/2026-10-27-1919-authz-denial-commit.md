---
status: approved
issue: 1919
spec: spec/2026-10-27-1919-authz-denial-commit.md
---

# Plan: write authz denials on their own committed session

Approved decisions: `_audit_authz_denial` writes through `log_audit_event_bg` (own
session, chained, committed, errors swallowed); its `db` parameter is dropped; the
403 is re-raised unchanged. Two steps, implemented by the session model (below the
coder handoff threshold).

## Steps

1. `apps/web-server/server/routes/project_authz.py:175-210`: call
   `log_audit_event_bg` without `db`; drop `db` from `_audit_authz_denial` and its
   call site; update its docstring.
   → verify by `cd apps/web-server && .venv/bin/ruff check server/routes/project_authz.py`
   Traps: keep the outer `try/except Exception` so the 403 always re-raises.
2. `tests/audit/test_security_event_coverage.py:74-130`: patch
   `db_engine.async_session_factory` to the test DB, roll the caller's session back
   instead of committing, read rows from a fresh session.
   → verify by `cd apps/web-server && .venv/bin/pytest ../../tests/audit/test_security_event_coverage.py`
   Traps: the test must fail on dev; ruff S101 bans bare `assert` there (use `_require`).

## Tests

- `tests/audit/` green; the changed test fails against dev's `project_authz.py`.

## Rollback

Revert the commit.

## Deviations

- Step 2: the SQLite test cannot show the bug. pysqlite emits no BEGIN, so the
  savepoint's RELEASE commits the row even after the request session rolls back;
  the SQLite test passed on dev. Added `tests/postgres/test_authz_denial_audit_pg.py`
  (CI `postgres-acceptance` job): a 403, then the request session rolls back, then
  exactly one `authz.denied` row. It fails on dev (`[] == ['authz.denied']`) and
  passes with the fix. The SQLite test keeps its rollback + fresh-session read so
  it exercises the new own-session path.
