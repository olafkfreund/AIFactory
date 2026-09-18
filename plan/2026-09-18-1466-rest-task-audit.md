---
status: approved
issue: 1466
spec: spec/2026-09-18-1466-rest-task-audit.md
---

# Plan: Audit task actions on the REST surface

## Decisions (carried from the approved spec)

- The REST handlers emit **one** `task.*` audit row per successful action.
- A call proxied by the MCP router emits **no** handler row. The proxy keeps writing its
  own `mcp.task.*` row (with `mcp_key_id`), so there is one row per action either way.
- The switch is `isinstance(_access, dict)`. A REST call has the resolved principal. A
  direct call from the proxy leaves `_access` at its `Depends(...)` default.
- The write goes through `log_audit_event_bg`, which is hash-chained, self-managed and
  never raises.
- No middleware. `_audit_mcp_write` is not refactored.

## Steps

1. `apps/web-server/server/services/audit_service.py`: add constants
   `ACTION_TASK_STOP="task.stop"`, `ACTION_TASK_RECOVER="task.recover"`,
   `ACTION_TASK_UPDATE="task.update"`, `ACTION_TASK_DELETE="task.delete"`,
   `ACTION_TASK_APPROVE_PLAN="task.approve_plan"`, `ACTION_TASK_CREATE_PR="task.create_pr"`,
   `ACTION_TASK_APPLY_CORRECTION="task.apply_correction"`, `ACTION_TASK_HANDOFF="task.handoff"`,
   `ACTION_TASK_DISPATCH="task.dispatch"`. Add the helper:
   ```python
   async def audit_task_action(access, action, task_id, request=None, details=None) -> None:
       if not isinstance(access, dict):
           return  # proxied MCP call: the mcp_stdio router writes its own mcp.task.* row
       ip = request.client.host if request is not None and request.client else None
       await log_audit_event_bg(user_id=access.get("id"), org_id=access.get("org_id"),
                                action=action, resource_type="task",
                                resource_id=task_id, details=details, ip=ip)
   ```
   → verify: unit test in step 5 (non-dict → no call; dict → one call with those fields).
2. `apps/web-server/server/routes/execution.py`: after each success path, before
   `return`, `await audit_task_action(_access, <ACTION>, task_id, raw_request_if_available)`:
   - `start_task` (:257) → `ACTION_TASK_START`, request=`raw_request`
   - `handoff_to_tfactory` (:857) → `ACTION_TASK_HANDOFF`
   - `stop_task` (:925) → `ACTION_TASK_STOP`
   - `recover_task` (:957) → `ACTION_TASK_RECOVER`
   - `create_and_run_task` (:1108) → `ACTION_TASK_CREATE`, task_id from the result,
     `details={"project_id":…, "title":…, "via":"create-and-run"}`, request=`raw_request`
   - `create_from_trusted_plan` (:1267) → `ACTION_TASK_CREATE`, `details.via="from-plan"`
   - `apply_task_correction` (:1453) → `ACTION_TASK_APPLY_CORRECTION`
   - `dispatch_task_to_copilot` (:1478) → `ACTION_TASK_DISPATCH`
   → verify by step 5.
3. `routes/plan_approval.py:55` `approve_plan` → `ACTION_TASK_APPROVE_PLAN`;
   `routes/pr.py:51` `create_pr_from_task` → `ACTION_TASK_CREATE_PR`;
   `routes/worktree_merge.py:1726` `merge_worktree` → `ACTION_TASK_MERGE`.
   → verify by step 5.
4. `routes/tasks.py`:
   - `update_task_status` (:377) and `update_task` (:473/474) → `ACTION_TASK_UPDATE`;
   - `delete_task` (:653) → `ACTION_TASK_DELETE`;
   - `create_task` (:238) takes no `_access`. Pass
     `getattr(request.state, "user", None) if request is not None else None`, and when that
     is `None` while `request is not None` (auth disabled), pass `{"id": None}` so the row is
     still written. `request is None` means a direct in-process caller, so skip. The proxy
     never calls `create_task` (verified).
   → verify by step 5.
5. New `tests/audit/test_task_action_audit.py`:
   - helper unit tests (non-dict skips; dict writes once with `user_id`, `org_id`, `ip`);
   - a parametrised test over every route in steps 2-4: a REST call via the test client →
     exactly one `log_audit_event_bg` call with the expected action and `resource_id`
     (patch the helper's sink, following the pattern in `tests/audit/conftest.py`);
   - a proxied MCP call (for example `/api/mcp-stdio/tasks/{id}/stop`) → exactly one call,
     with action `mcp.task.stop` and no `task.stop`;
   - the sink raising → the route still returns 2xx.
   → verify: `apps/backend/.venv/bin/pytest tests/audit/test_task_action_audit.py -v`.
6. `apps/web-server/openapi.yaml`: regenerate only if a route signature changed (none is
   planned). → verify: `git diff --stat` shows no openapi change.

## Tests

```bash
APP_DISABLE_AUTH=true apps/backend/.venv/bin/pytest tests/audit/ -v
apps/backend/.venv/bin/pytest apps/web-server/tests -k "mcp_stdio or execution or tasks" -q
```
Expected: all pass. The existing MCP audit tests are unchanged.

## Rollback

Revert the implementation commit. Audit rows already written stay, which is harmless and
append-only. There is no schema change.

## Deviations during implementation

- **Steps 2-3, many-return handlers.** `start_task` (six success returns),
  `create_pr_from_task` (10 returns) and `merge_worktree` (~35) are not edited at each
  return. Each became a thin audited wrapper keeping the route's name, signature,
  docstring and `Depends`, over an unaudited body `_<name>`. The proxy imports the
  route by name, so it is unaffected. `create_pr_from_task` and `merge_worktree` report
  failure as `{"success": False}` rather than raising, so their wrapper audits only
  when `result["success"]` is truthy. `start_task` has no such return, so it audits
  whenever the body did not raise.
- **Step 5, test shape.** Driving all 16 routes to their success path through the HTTP
  client needs a per-route project/agent fixture. The test file instead covers live:
  the helper, REST vs proxied `stop` (one `task.stop` row vs one `mcp.task.stop` row),
  `delete`, and all three wrappers, including the success-false case. The remaining
  routes get a source check that each calls `audit_task_action` with its action.
- **Found, out of scope.** `routes/changelog.py:1056` (create task from insights) calls
  `create_task` in-process with no request, so it stays unaudited, as it was before.

### Deviation: Postgres acceptance cleanup and typing (after the first CI run)

- **`tests/postgres/test_p1_suite_against_postgres.py`: the P1 runner now deletes the
  audit rows its inner suite wrote.** That suite runs every test with
  `APP_DISABLE_AUTH=true`, so `require_task_access` returns a real principal and the REST
  task routes write real `task.*` rows, with composite task ids longer than 36 chars. Left
  in the shared database, they made the later downgrade tests
  (`test_per_tenant_audit_anchor_schema`, `test_tenant_states_schema`) hit the
  `c1f5a3d7b924` guard, which correctly refuses to narrow `resource_id`. The guard is
  untouched. The cleanup is keyed on the row ids that existed before the run, not on a time
  window: `created_at` is a naive UTC timestamp and a non-UTC session kept the new rows. It
  tolerates a fresh database where `audit_logs` does not exist yet. Verified against a local
  Postgres 17: both downgrade tests went from failing to passing, 40/41 total. The one
  remaining local failure is five `tests/rmux/` tests that pass in isolation and in CI
  (a local-environment ordering effect, not this change).
- **mypy/ruff ratchet:** splitting routes into a wrapper and a body duplicated their existing
  untyped signatures. The new private bodies (`_start_task`, `_create_pr_from_task`,
  `_merge_worktree`) are typed `-> Any` with `X | None` defaults, and the helper uses
  `dict[str, Any]`. Public route signatures are left exactly as on `dev`, so the OpenAPI
  schema is unchanged. Both ratchets report 0 regressed.

### Deviation: a route decorator replaces the wrapper split (found by the full web-server suite)

The earlier deviation split `start_task`, `create_pr_from_task` and `merge_worktree` into
an audited wrapper around an unaudited `_<name>` body. Behaviour was intact, since
`@honest_status` stayed on the wrapper, but it moved each route's `{"success": False}`
returns out of the decorated function. #1126's completeness guard
(`apps/web-server/tests/test_route_refusals_are_not_http_200.py`) finds refusing routes by
reading each decorated route's own body. It lost them
(`test_the_inventory_is_not_empty` fell below 80), and it could no longer catch
`@honest_status` being removed from those routes.

Now: the three route bodies are **byte-identical to `dev`**, with one new decorator,
`audit_service.audit_task_route(action)`, stacked **under** `@honest_status` so it sees
the handler's raw dict. It writes a row only for a dict with truthy `success`, and binds
arguments to the route's signature, so the MCP proxy's direct positional calls (where
`_access` stays at its `Depends` default) are skipped. `functools.wraps` keeps FastAPI's
parameter and dependency resolution unchanged (checked on the built app: the same path,
body and `TaskAccessChecker` for all three). The `-> Any` typing added for the old bodies
is gone with them.

Verified: the #1126 guard passes (29), `tests/audit/` passes (103), the full
`apps/web-server/tests` has 0 failures, and ruff/mypy ratchets report 0 regressed.
Mutation-checked: removing the merge route's decorator fails the new structural test.
