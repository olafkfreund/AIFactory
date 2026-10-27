---
status: draft
issue: 1939
spec: spec/2026-10-27-1939-rmux-audit-commit.md
---

# Plan: write rmux console audit rows on their own committed session

Approved decisions: `attach` and `detach` in `apps/web-server/server/rmux/bridge.py`
roll the request session back (errors suppressed), then write
`console.attach` / `console.detach` with `log_audit_event_bg` (own session,
bounded lock wait, committed, errors swallowed). Responses unchanged. Two
steps, implemented by the session model (below the coder handoff threshold).

## Steps

1. `apps/web-server/server/rmux/bridge.py:61,278-288,323-333`: import
   `log_audit_event_bg` instead of `log_audit_event`; in both handlers
   `with contextlib.suppress(Exception): await db.rollback()` then
   `await log_audit_event_bg(...)` with the same fields, no `db`.
   → verify by `ruff check apps/web-server/server/rmux/bridge.py`
   Traps: keep the `db is not None` guard (tests call handlers with `db=None`);
   the audit stays outside `state.lock`.
2. `tests/postgres/test_rmux_console_audit_pg.py` (new): fixture as in
   `tests/postgres/test_authz_denial_audit_pg.py`; attach + detach with a
   request session, rollback, expect both rows.
   → verify by `pytest tests/postgres/test_rmux_console_audit_pg.py` against a
   Postgres (`TEST_POSTGRES_URL`); must fail on dev.
   Traps: own schema name; drop it on teardown (downgrade tests scan schemas).

## Tests

- `pytest tests/rmux/` green; the new Postgres test fails on dev, passes here.

## Rollback

Revert the commit.
