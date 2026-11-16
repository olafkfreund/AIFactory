---
status: approved
issue: 2024
author: olafkfreund
---

# Intent: Job log markers must not be able to forge terminal state

## Problem

`AIFACTORY_MARKER_NONCE` (#1929) is in the env of build Jobs, the spec Job
(#1932) and in-pod builds, because the runner must sign its own markers with
it. An agent that can read it (for example from the runner's
`/proc/<pid>/environ`) can print `__EXEC_PHASE__:<nonce>:{"phase":"complete"}`
or `"failed"`, and `agent_emit._handle_output_line` then sets the tracked phase
and emits the matching status event. This is status forgery, not a gate bypass:
exit code and review state come from the Job outcome.

## Proposed outcome

A phase marker, even one with a valid nonce, can no longer move a task to a
terminal state. Terminal state comes only from process/Job reconciliation.
Non-terminal progress markers keep working.

## Affected users and systems

`apps/web-server/server/services/agent_emit.py` (shared by the kubejob build,
spec Job and in-pod paths). No change to the Job manifests or the runner.

## Constraints

- Keep the nonce in the runner env: the runner needs it to sign.
- Must not change the marker wire format or the build emitters.
- Legitimate `complete`/`failed` markers still appear in the log pane.

## Open questions

None. Keeping the nonce out of the Job env entirely is rejected in the spec.
