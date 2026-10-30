---
status: draft
issue: 1946
author: olafkfreund
---

# Intent: re-check approval before the interactive Ctrl+C resume

## Problem

After Ctrl+C in an interactive `run.py` build, choosing "Continue without
instructions" (`_handle_build_interrupt` in `apps/backend/cli/build_commands.py`)
calls `run_autonomous_agent` again without the pre-flight approval check that
guards the first call. Since #1945 (#1940) `run_autonomous_agent` snapshots the
plan at entry and grants plan commands from that snapshot, so on this resume
the snapshot is taken from a plan the agent may have widened during the
interrupted run. No human approval covers the widened plan.

## Proposed outcome

A resume of a review-required spec whose approval no longer matches the plan
does not start the agent: it tells the user to re-approve and shows the usual
"TO RESUME" instructions. An unchanged approved plan resumes as today.

## Affected

Interactive CLI users only. Web UI and kubejob builds do not use this path.

## Constraints

- Same approval question as the pre-flight (`requires_review_before_coding`,
  `ReviewState.is_approval_valid`), no second definition.
- No new parameters on `run_autonomous_agent`.

## Open questions

None.
