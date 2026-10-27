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
- Review (lead, Opus), HIGH: each 403 held two pooled connections (the
  membership SELECT autobegins and keeps the request's connection while
  `log_audit_event_bg` takes a second), so a burst of denials could starve the
  pool. `authorize_project_for_user` now rolls the request session back before the
  audit write. Safe for every caller, checked on dev: `ProjectAccessChecker`
  (REST dependency), `mcp_stdio/auth.require_acw_access`, `mcp_remote/tools.py`,
  `rmux/bridge._authorize_console` (attach, detach, WS) and `from_issue` all
  authorize on a fresh session before any write. Test: after the 403 the request
  session is not in a transaction.
- Review, MEDIUM: the chain could fork under concurrent writers, and denials make
  the volume attacker-controlled. `_next_prev_hash` takes
  `pg_advisory_xact_lock` (no-op on SQLite) before reading the head, held to the
  writer's transaction end. Rows get `created_at = clock_timestamp()` on Postgres
  (`_chain_row_time`): the `now()` default is the transaction start, so a writer
  that waited would sort before the rows inserted while it waited. Own-session
  writers set `lock_timeout = '5s'` (`_bound_lock_wait`), so a request that already
  holds the lock through `log_audit_event` gets a logged audit failure instead
  of hanging. `services/llm_audit_hook.py` had its own copy of the head read; it
  now uses the same helpers. Test: 20 concurrent `log_audit_event_bg` writers
  give 20 distinct `prev_hash` values and a chain that verifies. On the previous
  head all 20 chained off GENESIS.
