---
status: approved
issue: 1771
author: olafkfreund
---

# Intent: a monitor that raises must still record a terminal status

## Problem

Still open on dev at `33f8433b`. Found by the Factory#971 terminal-status audit.

`monitor_process` (`apps/web-server/server/services/process_monitor.py:96`)
watches an in-pod subprocess. On a normal exit it writes the terminal status to
the plan and control files with `service._update_plan_status(..., status,
task_id, emit_events=False)` (`:791`). Its catch-all `except Exception`
(`:909-929`) only frees memory and emits a FAILED progress event. It never calls
`_update_plan_status`, so `implementation_plan.json` and `task_control.json`
keep their last non-terminal status.

The board reads that status (`routes/task_service.py:884-898`) and shows
`in_progress` while `is_running` is false, forever.

Deployed (kubejob executor, `DATABASE_URL` set, `REAPER_DRY_RUN=true`) builds run
as Jobs, but in-pod subprocesses such as spec creation
(`services/agent_spec_creation.py:338`) still go through this monitor. Nothing
heals it: the #1001 reaper skips tasks with no durable row
(`agent_kubejob.py:1072-1077`) and the stale reaper is dry-run.

## Outcome

When the monitor body raises, the task ends in the same terminal state as a
failed exit: control status `human_review`, reviewReason `errors`.

## Affected

- `process_monitor.py` exception handler only.
- One new regression test.

## Constraints

- The FAILED progress event and the memory cleanup must still happen even if
  the status write itself fails (a dropped write is logged loudly, not raised).
- `CancelledError` keeps its current behaviour (stop_task owns that path).
- Do not change `_update_plan_status`; a user-set `done` is still respected
  there.

## Open questions

None.
