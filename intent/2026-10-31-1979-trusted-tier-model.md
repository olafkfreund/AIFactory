---
status: draft
issue: 1979
author: olafkfreund
---

# Intent: token tier stamp and per-subtask model must not come from agent-writable files

## Problem

Two run-time values are still read from files the coding agent can edit
(follow-up to #1969, which left them out):

1. `agents/token_attribution.py` `record_turn` calls
   `load_task_metadata(spec_dir)` on the worktree spec dir to stamp
   `routing_tier` on each worker record in `token_usage.json`. An agent can
   mislabel the tier (telemetry only, low).
2. `agents/coder.py` (~463) and `agents/parallel_integration.py` (~594) take a
   coding subtask's `model` override (#376 right-sizing) from the live
   `implementation_plan.json`. Grants already come from the approved-plan
   snapshot (#1940), but the model does not, so an agent can change the model
   of a later subtask after plan review.

## Proposed outcome

A worktree edit of `task_metadata.json` or `implementation_plan.json` after
run start changes neither the stamped tier nor any subtask's model.

## Affected users and systems

AIFactory backend: serial coder, parallel wave executor, token attribution.

## Constraints

- Reuse the #1983 `RunGates` run-start snapshot and the #1940 approved-plan
  snapshot. No new snapshot mechanism.
- No change when plan review is off (no snapshot exists; the plan is not
  gated then, same as grants).
- Token accounting must never break a build.

## Open questions

None. The user decided: tier from `RunGates.metadata` (source spec dir);
subtask model from the approved-plan snapshot, falling back to the phase
default (not the live file) when the subtask is not in the snapshot.
