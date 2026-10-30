---
status: approved
issue: 1955
author: olafkfreund
---

# Intent: QA plan-command seeding reads the approval gate from the source spec dir

## Problem

`handle_qa_command` (`apps/backend/cli/qa_commands.py` ~118-127) grants the
plan's `required_commands` when review is not required or the approval is
still valid, reading both from the `spec_dir` it is given.

Confirmed caller path: `cli/main.py` passes the result of `find_spec`
(`cli/utils.py:55`). That is normally the source dir
`.aifactory/specs/<name>` (gate files are not agent-writable there; the plan is
synced back from the worktree, so `is_approval_valid` already hashes the plan
in use). But when the source spec is absent, `find_spec` falls back to the
worktree copy `.aifactory/worktrees/tasks/<name>/.aifactory/specs/<name>`,
whose `review_state.json` and `task_metadata.json` the agent can write. There
a forged approval or a flipped `requireReviewBeforeCoding` grants a widened
plan's commands. Same class as #1946 / #1952.

## Proposed outcome

The gate files are read from the source spec dir
(`get_specs_dir(project_dir) / spec_dir.name`); approval is hashed against the
plan that is seeded (`spec_dir`). No source dir → no plan grants (fail closed).

## Affected users and systems

`run.py --spec <name> --qa` in AIFactory. The normal source path is unchanged.

## Constraints

Never let seeding break QA (keep the existing broad except). QA itself still
runs; only plan-command grants are withheld.

## Open questions

None.
