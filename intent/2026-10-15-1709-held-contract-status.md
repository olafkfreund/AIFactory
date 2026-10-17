---
status: approved
issue: 1709
author: olafkfreund
---

# Intent: show a held task contract on the task status

## Problem

Open on `dev`; nothing on `dev` writes a hold onto a task. When the pod holds a
task contract (#1673, `apps/backend/core/contract_trust.py:66-97`), the only
trace is a `[trusted-contract]` log line and a `print_status` line:

- `apps/backend/cli/build_commands.py:203-221` (`_resolve_build_contract`):
  a held migration logs and calls `sys.exit(1)`; any other hold logs and the
  build carries on as a normal build.
- `apps/backend/agents/deploy_scaffold.py:91-95`: the deploy scaffold is
  withheld and only logged.

An operator looking at the task in the UI or the API sees a failed migration
(or a build with no deploy files) and no reason. #1673's plan deferred this as
D10: "A task-status field is a follow-up".

## Proposed outcome

- A hold puts a short, neutral note on the task (reason + what was held), and
  the task list API and the task detail header show it.
- The note never changes `status` or `reviewReason`, so a hold does not render
  or count as a build failure in the UI, CFactory sync or metrics.
- A later build whose contract is not held clears the note.

## Affected users and systems

- `apps/backend/core/contract_trust.py`, `cli/build_commands.py`,
  `agents/deploy_scaffold.py` (pod side).
- `apps/web-server/server/routes/task_service.py` (`task_to_dict`).
- `apps/frontend-web` Task type and `TaskHeader`.
- Consumers that branch on task status: unchanged (they never read the new key).

## Constraints

- Builds on #1711's explicit `legacy` verdict; reasons are only the constant
  labels from `reason_label` (CodeQL clear-text-logging heuristic).
- Best-effort: recording the note must never raise or change a build's outcome.
- Target `dev`, add a CHANGELOG entry.

## Open questions

1. A held migration still exits 1 (approved #1673 decision), so that task still
   ends as a failed build. Change that here? Conservative answer, taken here:
   no. This task is visibility only; the note says why it stopped.
