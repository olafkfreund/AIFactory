---
status: approved
issue: 1771
spec: spec/2026-10-15-1771-monitor-raise-terminal.md
---

# Plan: a monitor that raises must still record a terminal status

Self-contained. You do not need to open the intent or the spec.

**Problem.** `monitor_process`
(`apps/web-server/server/services/process_monitor.py:96`) records the terminal
status through `service._update_plan_status` only on a normal exit (`:791`).
Its `except Exception as e:` handler (`:909-929`) frees memory and emits FAILED
but writes no status, so the board shows `in_progress` forever. No reaper heals
it in the deployed config.

**Approved decisions.**

1. At the top of that handler, call
   `service._update_plan_status(project_path, spec_id, "failed", task_id, emit_events=False)`
   guarded by `if spec_id and project_path:` (same as the normal exit).
2. Wrap that call in `try/except Exception` with `logger.exception(...)` so a
   failing write never skips the cleanup or the FAILED event.
3. Leave the cleanup, the `_emit_progress(FAILED)` and the `CancelledError`
   branch unchanged. Do not touch `_update_plan_status`.

## Steps

### Step 1 — regression test

File: `tests/test_issue_1771_monitor_raise_terminal.py` (new).

- `sys.path.insert(0, <repo>/apps/web-server)`; import `AgentService`,
  `TaskPhase` from `server.services.agent_service`, `task_control` from
  `server.services`, `monitor_process` from `server.services.process_monitor`.
- Build `AgentService()`; seed
  `<tmp>/.aifactory/specs/001/implementation_plan.json` with one phase holding
  one subtask and `status: in_progress`, and write control status
  `in_progress` via `task_control.write_control`.
- Patch `service._emit_progress` with an `AsyncMock`; patch
  `server.services.completion_orchestration.run_terminal_completion` with an
  `AsyncMock` (keeps the test off the network).
- A fake proc whose `async wait()` raises `RuntimeError("boom")`.
- Await `monitor_process(service, "proj:001", proc, tmp, "001")`.
- Assert `read_control(spec_dir)` status `human_review`, reviewReason
  `errors`; assert the emitted progress phase is `TaskPhase.FAILED`.

Check: `apps/backend/.venv/bin/pytest tests/test_issue_1771_monitor_raise_terminal.py -q`
fails on the unfixed code (control still `in_progress`).

Trap: `_update_plan_status` returns early without a control write when the plan
has no `phases`; the fixture plan must have one.

### Step 2 — the fix

File: `apps/web-server/server/services/process_monitor.py`, the
`except Exception as e:` block (`:909`). Insert decisions 1–2 before the first
cleanup line. Use the module's existing `logger` and `sanitize_log`.

Check: the step 1 test passes; also run
`tests/test_terminal_completion_characterization.py` and
`apps/web-server/tests/test_agent_service_failover.py`;
`ruff check` and `ruff format --check` on both files.

## Rollback

Revert the single fix commit; the test commit goes with it.
