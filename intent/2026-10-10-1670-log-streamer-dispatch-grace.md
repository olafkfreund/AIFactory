---
status: draft
issue: 1670
author: olafkfreund
---

# Intent: the log streamer asks the same "is it alive" question as `is_running()`

Follow-up deferred by answer 2 of
[intent/2026-10-08-1662-kubejob-active-at-dispatch.md](2026-10-08-1662-kubejob-active-at-dispatch.md).

## Problem

There are two answers to "is this kubejob build still alive?".

- `AgentService.is_running()` (`agent_service.py:1569`) reads
  `_active_kubejob_task_ids` and nothing else.
- The log streamer's check, `_kubejob_still_active`
  (`agent_kubejob.py:625-646`), reads the same set. It also says "alive" for
  any id that is missing from the set during the first 45 seconds
  (`_DISPATCH_GRACE_SECONDS`, `agent_kubejob.py:32-37`, from #1619).

That grace period covered the wait before the first reconcile tick added a new
build to the set. Since #1662 (PR #1678), dispatch adds the id itself
(`agent_kubejob.py:371-372`), before the only streamer start (line 377). The
tick keeps it (line 733), and pending rows show up on the next tick (#1606).
So the grace branch is never reached in a real build. Only tests reach it.

The cost is not visible to users today. It is a second liveness rule, with a
docstring that justifies it by a restart case that can no longer reach a
streamer. If the set is ever wrong for some other reason, the streamer keeps
reattaching for up to 45 seconds while the cockpit says "not running". That
hides the very mismatch #1619 was about. Two tests lock in the old rule
(`tests/test_is_running_kubejob.py:162-201`).

## Proposed outcome

- The streamer's liveness check and `is_running()` give the same answer for
  every task id, because both read `_active_kubejob_task_ids` only.
- An id that dispatch never marked reads as inactive to the streamer.
- A build that dispatched successfully streams its logs exactly as it does
  today.
- No time-based liveness rule remains, and no docstring describes one.
- The tests assert the new behaviour without patching a grace constant.

## Affected users and systems

- `apps/web-server/server/services/agent_kubejob.py` (the grace constant and
  `_kubejob_still_active`).
- `tests/test_is_running_kubejob.py` (the two tests above).
- Unchanged: `build_log_stream.py`, `agent_service.py`, `job_state_store.py`.
- People: anyone reading or debugging kubejob liveness, and users who watch
  live build logs in the cockpit.

## Constraints

- Must keep the dispatch order: the id is added after the dispatch `except`
  block, and the streamer starts after that (`agent_kubejob.py:366-377`).
- Must not change the reconcile tick (clear before the first await, replace
  the set with `live | dispatched_this_tick`, keep the old set on a failed
  store read).
- Must not change the `build_log_stream.py` contract: `job_active=None` still
  means one pass, and `_MAX_EMPTY_REATTACHES` stays as the backstop.
- The streamer must not read the store or poll the backend on its own.
- Must not add shared state or assume more than one replica (#1669;
  `replicaCount: 1` is pinned at `charts/aifactory/values.yaml:32`).
- Must not touch the dispatch-raised-after-Job-created case (#1677).
- No orphaned code: nothing left unused or unreferenced (Ruff F401).
- No public API change. The `job_active` callable keeps its shape.
- No trust boundary is involved: the change is in-process state only.

## Open questions

1. Known behaviour change: when `reconcile_by_poll` raises for a build
   (`agent_kubejob.py:698-700`), the id drops out of the set. Today the grace
   window hides that for a new build's first 45 seconds. Without it, one
   transient poll error ends live-log following at the next end-of-stream,
   as `is_running()` already reports. Accept it, or open a separate issue?
2. Does this internal clean-up get a CHANGELOG entry? If so, expect a rebase
   conflict with open PR #1691, which also edits `CHANGELOG.md`.
3. Land #1670 on its own now, or batch it with #1669 and #1677?
