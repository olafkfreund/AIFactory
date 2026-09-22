---
status: draft
issue: 1569
author: Olaf Krasicki-Freund
---

# Intent: A task's creation date is whenever something last wrote to it

## Problem

`spec_to_task` reports a task's `created_at` as the spec directory's **inode change
time** (`routes/task_service.py:960`, `datetime.fromtimestamp(stat.st_ctime)`). `st_ctime`
moves on any write inside the directory — a control-plane status write, an agent sync, a
restore or copy — so it is not a creation time at all.

Observed on 2026-09-18 (#1569): 18 tasks created between 09-08 and 09-11 all reported
`created_at` 2026-09-18T13:02, the moment the merger wrote their `task_control.json`.
Before that write they all read 2026-09-15T12:45, a node restart. The real dates were
never visible.

Anything that orders or ages tasks by this value is wrong in the same way. `routes/tasks.py:186`
sorts the board by `created_at` descending, so a recently touched old task presents as
the newest one; "stale task" heuristics and cockpit timelines read the same field.

**The true value is already recorded and simply not read.** Every task-creation path
stamps `created_at` into `requirements.json` at intake:

- `routes/tasks.py:287` (API create)
- `routes/projects.py` `create_project_task` (the web UI's path)
- `routes/execution.py:1164` (create-and-run) and `:1356` (from-plan)

## Proposed outcome

A task's reported creation date is the moment it was created, and it does not change when
anything writes to the spec directory afterwards. Board order and age heuristics follow the
real dates. Where no stamp exists (older specs, CLI-created ones), the reported value is
still a best effort and is identifiable as such rather than silently presented as fact.

## Affected users and systems

- `apps/web-server/server/routes/task_service.py` (`spec_to_task`), and every reader of
  `Task.created_at`: the board ordering in `routes/tasks.py:186`, the task list/detail UI,
  CFactory timelines and any staleness heuristic.
- Existing specs with no stamp — the ones this was noticed on.

## Constraints

- Read-only change to task data: no rewriting of `requirements.json`, and no backfill that
  invents a date.
- A malformed or missing stamp must not break task listing; it falls back, and the fallback
  must not be presented as a real creation time.
- `updated_at` keeps using `st_mtime`, which is what it means.
- The `Task` API shape is consumed by the frontend and CFactory, so any new field is additive.

## Open questions

1. **How should a fallback be reported?** Options: an additional boolean such as
   `created_at_is_estimate`, or leaving `created_at` null so consumers decide. Null is
   honest but may break sorting in existing consumers.
2. **Backfill?** For the ~18 known specs the true date can often be recovered (the spec's
   first git commit, or `requirements.json`'s own stamp where present). Worth a one-off
   script, or leave them on the fallback?
