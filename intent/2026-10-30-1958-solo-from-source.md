---
status: approved
issue: 1958
author: olafkfreund
---

# Intent: a resumed run reads solo mode from the source spec dir

## Problem

`run_autonomous_agent` (`apps/backend/agents/coder.py` ~212) computes `solo`
from `spec_dir`. In worktree mode that is the agent-writable worktree copy, so
an agent can set `soloMode` in the worktree `task_metadata.json` and, on a
Ctrl+C resume, get the solo flow (solo prompt, no dedicated planner, lighter
review tier). #1952 already reads `require_review` from the source copy;
`solo` was left behind.

## Proposed outcome

`solo` and `require_review` both come from `source_spec_dir or spec_dir`. A
flipped worktree `soloMode` does not change the resumed run's flow.

## Affected users and systems

AIFactory backend builds in worktree mode. Direct mode is unchanged (one dir).

## Constraints

No behaviour change when the worktree and source metadata agree. The env
override `AIFACTORY_SOLO_MODE` still wins.

## Open questions

None.
