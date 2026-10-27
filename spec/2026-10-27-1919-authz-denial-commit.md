---
status: approved
issue: 1919
intent: intent/2026-10-27-1919-authz-denial-commit.md
---

# Spec: write authz denials on their own committed session

## Design

`_audit_authz_denial` calls `log_audit_event_bg` (`services/audit_service.py`) instead
of `log_audit_event(db, ...)`. `log_audit_event_bg` opens its own session from
`engine.async_session_factory`, chains the row (`_next_prev_hash`), commits and
swallows its own errors. A denial has no business transaction to share, so the
"same transaction" reason `log_audit_event` uses a savepoint does not apply. The
`db` parameter of `_audit_authz_denial` goes away; the outer `try/except` stays so
the 403 is always re-raised.

`_next_prev_hash` takes no lock, so a second connection cannot deadlock against the
request session (which has only read `OrgMember`).

## Alternatives rejected

- `await db.commit()` after the savepoint: commits whatever else the caller had
  pending on its session, which is the caller's business.
- A commit in each caller's exception handler: three call paths (REST, stdio MCP,
  remote MCP) and easy to miss in a new one.

## Risks

- One extra short DB connection per denial. Denials are rare; the pool absorbs it.
- On SQLite (tests, single-node dev) a second connection must be able to write while
  the request session holds a read; the real-DB test proves it.

## Verification

`tests/audit/test_security_event_coverage.py::test_authz_denial_writes_chained_audit`
stops committing the caller's session (it rolls back, like `get_db`) and reads the
rows from a fresh session: exactly one chained `authz.denied` row and a 403. It fails
on dev (0 rows) and passes with the fix.
