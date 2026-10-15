---
status: approved
issue: 1693
author: olafkfreund
---

# Intent: a per-row poll error does not mark a live kubejob build as stopped

## Problem

`reconcile_kubejob_builds` (`apps/web-server/server/services/agent_kubejob.py:765-829`)
rebuilds `_active_kubejob_task_ids` from scratch every tick (`:825`). When
`backend.reconcile_by_poll(job_id)` raises for one row, the per-row
`try/except/continue` (`:790-794`) skips that row, so it is missing from
`live` and drops out of the set for the tick.

Since #1670 removed the 45 s dispatch grace, `_kubejob_still_active`
(`:720-736`) reads only that set. So one transient poll error makes
`is_running()` false for a build that is still running: the log streamer stops
at its next end-of-stream, the cockpit badge offers Recover, and the
`/recover` guard from #1619 opens. `_MAX_EMPTY_REATTACHES`
(`build_log_stream.py:91`) is the only backstop.

## Proposed outcome

A row whose poll raises keeps the answer it had last tick: if it was in the
active set, it stays in; if it was not, it stays out.

## Affected users and systems

- `AgentService.is_running` and its readers (cockpit badge, `/recover` and
  `/start` guards, the log streamer's liveness check).
- Only the kubejob build backend.

## Constraints

- The set must still be replaced wholesale each tick (#1619): a row gone from
  the store, or polled terminal, must leave the set.
- No new state; reuse last tick's set.

## Open questions

1. Should a row that never polled successfully join the set when its first
   poll raises? Conservative answer: no, it keeps its previous membership
   (absent), matching "keep last tick's answer". Dispatch-time marking (#1662)
   already covers a fresh dispatch.
