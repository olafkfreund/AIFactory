---
status: approved
issue: 1550
author: Olaf Krasicki-Freund
---

# Intent: The gate evidence marker is read from a different tree than it is written to

## Problem

The trailing-gate evidence marker can be written under one spec dir and read from
another. `agents/gate_runner.py::gate_dir_for` only knows the linked worktree
`.aifactory/worktrees/tasks/<id>`. On the kubejob/build-clone path, the marker is written
under the Job's nested worktree, which that function never derives.

Known effect: `services/merger.py` reads the marker from the source spec dir, so its PR
body reports gate evidence as absent when it exists. Today this is a false negative
(it under-claims verification). It belongs to the same class as #1538, though:
machinery that is truthful about the wrong tree. The #1496 guard in
`tools_pkg/tools/qa.py` reads the same marker.

## Proposed outcome

The writer and every reader of the gate marker resolve the same directory on every
execution path: local, linked worktree and kubejob/build-clone. A PR body states the
evidence that actually exists.

## Affected users and systems

- `apps/backend/agents/gate_runner.py` (`gate_dir_for`, `write_trailing_gate_marker`,
  `trailing_gate_marker_is_current`)
- `apps/backend/agents/tools_pkg/tools/qa.py` (the #1496 QA-approval guard)
- `apps/web-server/server/services/merger.py`
- Deployed kubejob builds

## Constraints

- A reader that cannot find the marker must report "no evidence", never invent any
  (keep the #1496 guarantee).
- Must not change the behaviour of the local/linked-worktree path, which is correct today.

## Open questions

1. Is the right fix to make `gate_dir_for` know the nested kubejob layout, or to have the
   Job sync the marker back to the source spec dir (like `_sync_worktree_files`)?
