---
status: approved
issue: 1924
author: olafkfreund
---

# Intent: a plan approval survives status updates and goes stale only on real plan changes

Refs olafkfreund/Factory#1960.

## Problem

`ReviewState.approve()` (`apps/backend/review/state.py`) stores an MD5 of the raw
bytes of `spec.md` and `implementation_plan.json`. Many writers change the plan file
without changing what the reviewer approved:

- `POST /tasks/{id}/approve-plan` rewrites `status`/`planStatus` and pops
  `reviewReason` straight after approving, so the approval is stale at once.
- The coder's `update_subtask_status` tool writes subtask `status`, `notes`,
  `updated_at`, `started_at` and the plan's `last_updated` during every build.
- QA, recovery, the worktree sync and the status routes write `qa_signoff`,
  `status`, `reviewReason`, `actual_output` and similar.

`is_approval_valid()` then returns False. It fails closed, but every restart without
`force` (recover, process_monitor auto-restart, plain start through run.py's
pre-flight) pauses the task in `human_review` again, and the injection-scan escape
hatch (`agents/coder.py`) loses the operator's approval.

## Proposed outcome

- Right after approve-plan, and after any number of status/progress writes,
  `is_approval_valid()` is True.
- Any change to what a reviewer approves (spec.md, feature, phases, subtask
  descriptions, files, verification, acceptance criteria, any unknown field) still
  makes it False.

## Affected users and systems

AIFactory backend `review/state.py`, and through it run.py's pre-flight,
`agents/coder.py`'s injection-scan check, QA's approval check and the web-server
start route (#1926: StartTask forces on `is_approval_valid(main)` or
`task_control.planApproved`).

## Constraints

- Fail closed: only fields known to be runtime progress are ignored; an unknown or
  new field still invalidates the approval.
- No change to the #1926 gate or to `task_control.planApproved`.
- Approvals stored before this change carry a raw-bytes hash and read as stale once
  (they were already stale in practice).

## Open questions

None.
