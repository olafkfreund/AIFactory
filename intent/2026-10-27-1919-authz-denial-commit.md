---
status: approved
issue: 1919
author: olafkfreund
---

# Intent: project-authz denials land in the audit log

Refs olafkfreund/Factory#1960.

## Problem

`authorize_project_for_user` (`apps/web-server/server/routes/project_authz.py`) writes
the `authz.denied` / `gate.rejected` row through `log_audit_event` on the caller's
request-scoped session (a SAVEPOINT), then re-raises the 403. Nothing commits after a
denial and `get_db` only closes the session, so the row is rolled back. REST
(`require_project_access` / `require_task_access`), stdio MCP (`require_acw_access`)
and remote MCP all lose their denials. The existing test hides this by calling
`commit()` itself after the denial.

## Proposed outcome

A denied request leaves exactly one chained `authz.denied` (or `gate.rejected`) row,
and the caller still gets the same 403.

## Affected users and systems

AIFactory web-server: every path that calls `authorize_project_for_user`; audit
consumers of Factory#313's chained log.

## Constraints

- The 403 is raised unchanged even if the audit write fails.
- The caller's session and transaction are not touched.
- No schema change.

## Open questions

None.
