---
status: approved
issue: 1939
author: olafkfreund
---

# Intent: rmux console attach/detach audit rows land in the audit log

Refs olafkfreund/Factory#1960.

## Problem

`apps/web-server/server/rmux/bridge.py` `attach` and `detach` write
`console.attach` / `console.detach` through `log_audit_event(db, ...)`, a
savepoint on the request session, and never commit. `get_db` only closes the
session, so on Postgres the rows are rolled back. Same failure as #1919
(fixed for authz denials in #1936). SQLite hides it: pysqlite emits no BEGIN,
so the savepoint's RELEASE commits. The request session also holds the
audit-chain advisory lock (#1936) until `get_db` closes.

These are the only `log_audit_event` call sites in `apps/`.

## Proposed outcome

A successful attach leaves exactly one chained `console.attach` row, a holder's
detach exactly one `console.detach` row, on Postgres. Responses are unchanged.

## Affected users and systems

AIFactory web-server rmux live console; audit consumers of the chained log.

## Constraints

- Attach/detach behaviour and status codes unchanged; an audit failure never
  fails the request.
- #1936's lock rules hold: the request session must not hold the chain lock
  while waiting on an own-session writer.
- No schema change. Do not touch `apps/backend/review/state.py` or
  `security/plan_commands.py`.

## Open questions

None.
