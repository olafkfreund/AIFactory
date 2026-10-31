---
status: draft
issue: 1997
author: olafkfreund
---

# Intent: Remaining agent-writable reads in the run-start gate snapshot

Follow-up to #1979 (PR #1996). Refs #1983, #1940.

## Problem

`RunGates.capture` (`apps/backend/run_gates.py`) still takes some values from
files an earlier agent session could have written:

1. With no source spec dir, `gate_dir` is the run's own spec dir. A previous
   run's agent can write `requireReviewBeforeCoding: false`, `mode` or
   `soloMode` into its `task_metadata.json`. The next run then turns plan
   review off, and grants and subtask models come from the live plan. The
   web server reaches this path: the subprocess backend with
   `auto_continue=false` shows the workspace menu, and "direct" gives no
   source dir. The CLI's `--direct` reaches it too.
2. The approved-plan snapshot is read from `spec_dir` (the worktree copy),
   not `gate_dir`, unlike every other gate value.
3. `get_phase_model_betas` and `get_fast_mode` (`apps/backend/phase_config.py`)
   read the live `task_metadata.json`.

## Proposed outcome

- A server-driven run with no source spec dir requires plan review and runs
  without solo mode, whatever the spec dir's metadata says.
- A CLI run with no source spec dir behaves as today and logs one warning.
- The approved-plan snapshot comes from `gate_dir`, once, at run start.
- Betas and fastMode: no production caller exists, so nothing changes. The
  spec records this.
- For each value, a test shows that editing the worktree before or during the
  run does not change it.

## Affected users and systems

AIFactory backend `run.py` builds: the subprocess and kubejob backends, and
local CLI use. No web-server, cluster or gitops change.

## Constraints

- Use a signal the server already sets; add no new heuristic. The signal is
  `AIFACTORY_TASK_ID`, which both dispatch paths set (#1972).
- Do not fail closed on the CLI's `--direct`: the user runs it locally, and
  by design it has no isolation.
- The snapshot stays a single read at run start.

## Open questions

None. The lead decided all three items.
