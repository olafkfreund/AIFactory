---
status: approved
issue: 1939
intent: intent/2026-10-27-1939-rmux-audit-commit.md
---

# Spec: write rmux console audit rows on their own committed session

## Design

In `attach` and `detach` (`apps/web-server/server/rmux/bridge.py`), replace
`log_audit_event(db, ...)` with the #1936 pattern: roll the request session
back (errors suppressed), then `log_audit_event_bg(...)`. The request session
has only run `_authorize_console`'s membership read, so nothing is lost. After
the rollback it holds no connection and no lock, so `log_audit_event_bg`
(own session, `lock_timeout = '5s'`, chain lock, commit, errors swallowed)
cannot wait on the request's own lock. The attach state change happens before
the audit write, as today; the response is unchanged.

## Alternatives rejected

- `await db.commit()` after `log_audit_event`: one line, but a commit error
  (dropped connection) would 500 an attach that is already in effect;
  `log_audit_event_bg` swallows its own errors. It also diverges from #1936.
- `log_audit_event_bg` without the rollback: two pooled connections per
  attach while the request's read transaction stays open.

## Risks

- One short extra connection per attach/detach, after the request's is
  returned. Attach needs `member`; volume is low.

## Verification

`tests/postgres/test_rmux_console_audit_pg.py`: call `attach` then `detach`
with a request session, roll that session back like `get_db`, read from a
fresh session: exactly `["console.attach", "console.detach"]`. Fails on dev
(no rows), passes with the fix. `tests/rmux/` stays green.
