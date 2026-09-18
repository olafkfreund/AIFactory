---
status: approved
issue: 1466
author: Olaf Krasicki-Freund
---

# Intent: Task actions on the plain REST API emit no audit event

## Problem

The only audit sink for task actions is `server/mcp_stdio/router.py::_audit_mcp_write`.
Create-and-run, start, stop, recover, approve-plan, create-pr and merge write an audit row
when they arrive through `/api/mcp-stdio/...`, and **no row at all** when the same action
arrives through `/api/tasks/...` (`server/routes/tasks.py` and `routes/execution.py` never
call `log_audit_event`). The web UI uses the REST surface, so most real task actions are
unaudited. In the audit log, this is indistinguishable from a failed write (#1458).

## Proposed outcome

The same task action produces the same audit row whichever API surface it came through,
with the actor and tenant recorded.

## Affected users and systems

- `apps/web-server/server/routes/tasks.py`, `routes/execution.py`, `mcp_stdio/router.py`
- `server/services/audit_service.py`
- Operators and compliance consumers of `audit_logs`

## Constraints

- An audit write failure must not break the task action, but must be logged, not swallowed.
- Must not double-write when the MCP proxy forwards to a REST handler.
- Tenant scoping of audit rows stays as it is today.

## Open questions

1. Should the MCP-side audit move down into the shared handlers, so there is one
   emission point instead of two?
