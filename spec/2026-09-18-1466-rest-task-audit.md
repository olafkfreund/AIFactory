---
status: draft
issue: 1466
intent: intent/2026-09-18-1466-rest-task-audit.md
---

# Spec: Audit task actions on the REST surface

## Design

Recommended answer to the intent's open question: **emit in the REST handlers, and write
exactly one row per action**. The MCP proxy keeps its own `mcp.task.*` row for the calls it
proxies, and the handler skips its row in that case.

How "one row" works without new parameters: the REST handlers take the caller through
`_access = Depends(require_task_access(...))` (`routes/project_authz.py:260`), which
resolves to the principal dict. The MCP proxy (`mcp_stdio/router.py`) calls the same
handler functions **directly**, where `_access` is left at its default, the `Depends`
marker, not a dict. So:

- REST call → `_access` is a dict → the handler emits `task.*`.
- MCP proxied call → `_access` is not a dict → the handler emits nothing, and the proxy
  emits `mcp.task.*` with `mcp_key_id`, as today.

One helper in `services/audit_service.py`:

```python
async def audit_task_action(access, action, task_id, request=None, details=None) -> None:
    if not isinstance(access, dict):
        return  # proxied call: the MCP router writes its own mcp.task.* row
    await log_audit_event_bg(user_id=access.get("id"), org_id=access.get("org_id"),
                             action=action, resource_type="task", resource_id=task_id,
                             details=details, ip=request.client.host if request and request.client else None)
```

`log_audit_event_bg` is used because it is already hash-chained (Factory#313), manages
its own session, and swallows its own failures with a warning. That meets "must not break
the action, must not be silent".

Actions and call sites, emitted after the action succeeds:

| Route | Action constant |
|---|---|
| `POST /api/tasks/create-and-run` (`execution.py:1108`) | `ACTION_TASK_CREATE` (exists) |
| `POST /api/tasks/from-plan` (`:1267`) | `ACTION_TASK_CREATE` + `details.via="from-plan"` |
| `POST /api/tasks/{id}/start` (`:257`) | `ACTION_TASK_START` (exists) |
| `POST /{id}/stop` (`:925`), `/recover` (`:957`) | new `task.stop`, `task.recover` |
| `POST /{id}/apply-correction`, `/handoff-tfactory`, `/dispatch-to-copilot` | new `task.apply_correction`, `task.handoff`, `task.dispatch` |
| `POST /{id}/approve-plan` (`plan_approval.py:55`) | new `task.approve_plan` |
| `POST /{id}/worktree/create-pr` (`pr.py:51`) | new `task.create_pr` |
| `POST /{id}/worktree/merge` (`worktree_merge.py:1726`) | `ACTION_TASK_MERGE` (exists) |
| `POST/PUT/PATCH/DELETE` in `routes/tasks.py` | `task.create`, new `task.update`, `task.delete` |

`ACTION_TASK_CREATE/START/MERGE` already exist in `audit_service.py:62-64` and are used
nowhere, which confirms the gap.

## Alternatives rejected

- **Middleware that audits every POST under `/api/tasks`.** It has no knowledge of
  success, task id or action semantics, and would audit reads that happen to be POSTs.
- **Move the MCP audit into the handlers and delete `_audit_mcp_write`.** Several proxy
  calls pass no request or principal (`stop_task(task_id)`), so the handler could not
  attribute them to the MCP key. It is a larger refactor for the same outcome.
- **Write both rows for proxied calls.** It double-counts every MCP action in the log.

## Risks

- A handler that does not take `_access` would silently skip. The plan lists each route's
  signature and adds `_access` wherever it is missing. A test asserts one row per route.
- `org_id` may be absent on the principal for service tokens. The row is still written,
  with `org_id=None`, matching the MCP rows for the legacy admin key.

## Verification

- A parametrised test over every route above: a REST call produces exactly one `audit_logs`
  row with the right action, `resource_id` and `user_id`.
- Proxied MCP calls to the same actions still produce exactly one `mcp.task.*` row (existing tests).
- Patching `log_audit_event_bg` to fail leaves the task action returning 2xx.
