---
status: draft
issue: 1662
intent: intent/2026-10-08-1662-kubejob-active-at-dispatch.md
---

# Spec: a kubejob build counts as running from the moment it is dispatched

## Design

The change has two parts. Both are in `apps/web-server/server/services/agent_kubejob.py`,
with one new attribute in `agent_service.py`.

### 1. Mark the task active when dispatch succeeds

In `_dispatch_build_job`, right after the `try/except` around
`self._build_backend().dispatch(...)` (`agent_kubejob.py:347-365`), add:

```python
self._active_kubejob_task_ids.add(task_id)
self._kubejob_dispatched_this_tick.add(task_id)
```

The lines go after the `except` block, so a dispatch that raised never marks
the task. They go before `_start_kubejob_log_stream` (`:370`), so the
streamer's liveness check (`_kubejob_still_active`, `:618-638`) sees the task
in the set as well.

`backend.dispatch` awaits `store.set_worker_ref(...)` before it returns
(`build_backend.py:1289-1299`). Per #1628, that call also sets the row to
`lifecycle_state="running"` (`job_state_store.py:404-421`). So by the time the
task is added to the set, its durable row is already one that
`get_active_kubejobs` returns (`job_state_store.py:445-492`).

`is_running` (`agent_service.py:1554`) does not change.

### 2. Close the race between a tick and a dispatch

`reconcile_kubejob_builds` (`agent_kubejob.py:665-723`) awaits the store read
at `:678` and the per-row polls at `:687`. If a dispatch finishes while those
awaits are in flight, the tick's `rows` were read before `set_worker_ref`
committed. Those rows do not include the new task. On the `/start` path the row
was still `done` (#1628), and on a fresh admit it might not exist yet. The
assignment at `:719` would then remove the task that dispatch just added.

The fix is a second set, `_kubejob_dispatched_this_tick: set[str]`, declared
next to `_active_kubejob_task_ids` in `AgentService.__init__`
(`agent_service.py:107`) and typed in the mixin's attribute block
(`agent_kubejob.py:83`):

- At the start of `reconcile_kubejob_builds`, after the backend-enabled check
  and before the first `await`, reset it with
  `self._kubejob_dispatched_this_tick = set()`.
- Replace the assignment at `:719` with
  `self._active_kubejob_task_ids = live | self._kubejob_dispatched_this_tick`.

This is correct because asyncio runs on a single thread. Nothing can interleave
between the reset and the store read, so each id in the set had its
`set_worker_ref` commit after this tick's read began. Those are the ids the
tick could have missed. Any dispatch that finished before the reset is
already committed, so this tick's own rows include it.

Each id stays in the set for one tick at most. The next tick resets the set,
reads rows that now include the task, and then keeps the task if its Job is
alive or drops it if the Job has finished. Nothing is ever added to the set
piece by piece across ticks, so the #1619 wholesale-replacement rule still
holds. Drift is limited to one tick, which matches today's latency for a build
that finishes.

When the store read fails (`:677-681`), the early return still leaves the
previous set in place. Dispatch has already added the task to that set, so it
stays visible.

### Accepted gaps (approved answers to intent questions 1 and 2)

- **After a restart, until the first tick.** The loop runs a tick at startup
  (`agent_kubejob.py:769-771`, started at `main.py:220`). Until that tick
  finishes, the in-memory set is empty and `is_running` is false for builds
  that were dispatched before the restart. Accepted. The gap is one tick long.
- **The log streamer's own grace period.** `_DISPATCH_GRACE_SECONDS`
  (`agent_kubejob.py:32-37`) stays. Changing `_kubejob_still_active` to read
  only the set is a follow-up issue.
- **Requests served by a replica that did not dispatch.** Each replica keeps
  its own set, so a different replica only sees the task after its own next
  tick. `replicaCount` is pinned to 1 (`charts/aifactory/values.yaml:6`), so
  this gap cannot happen today. It is recorded for when that pin is lifted.

## Alternatives rejected

- **Only add to the set at dispatch, with no race fix.** This leaves the race
  that intent question 3 put in scope: a tick in flight still clears the
  freshly added task.
- **Merge the old set into the new one (`live | old`).** The set would only
  ever grow, which is the drift #1619 forbids. Finished builds would read as
  running forever and block `/start`.
- **A dispatch timestamp map with a grace period in `is_running`**, like
  `_DISPATCH_GRACE_SECONDS`. This adds a third liveness rule and a time-based
  rule that the cockpit and the 409 would depend on. It still cannot tell a
  dead dispatch from a slow one. The per-tick set is exact and has no tuning
  knob.
- **Have `is_running` read the store or call Kubernetes.** The intent forbids
  this: the cockpit polls `is_running`, and the function must stay synchronous.
- **Run a reconcile tick straight after dispatch.** This adds a store
  round-trip and a Kubernetes poll to every dispatch. It also does not solve
  the race with a tick that is already running.
- **An `asyncio.Lock` around the tick and dispatch.** A lock would block
  dispatch behind a slow tick that polls every row. The set closes the race
  without making anything wait.

## Risks

- **A task stays marked for one extra tick if dispatch succeeds but the Job
  dies at once.** The next tick reads the row and drops the task. In the
  meantime, recover returns a 409 that `force` overrides. This is no worse than
  how a finished build behaves today.
- **The order of operations matters.** If the add came before
  `backend.dispatch` or inside its `try`, a failed dispatch could mark the
  task active. The tests pin the order.
- **Hosts affected.** Only the kubejob backend (the live default per #671).
  The subprocess backend does not use either set.
- **The test fake drifting from the real store.** The tests fake
  `get_active_kubejobs`. The race test needs a fake whose store read awaits a
  gate that the test controls, so the dispatch can land mid-tick.

## Verification

Write the tests first in `tests/test_is_running_kubejob.py`, reusing its
`_service` and `_StillRunningBackend` fixtures. The first two tests below must
fail on `origin/dev` before the code changes.

1. **Dispatch gap.** Build `_service(rows=[])`, and stub `_build_backend()` so
   that `dispatch` returns a Job name. Stub `_write_skill_context`,
   `_resolve_claude_token_pooled`, `_start_kubejob_log_stream` and
   `_safe_emit_task_status`. Await `_dispatch_build_job(...)`, then assert
   `service.is_running(task_id) is True`. Also call
   `_refuse_recovery_while_running(task_id, service, force=False)` and assert
   it raises `HTTPException` with status 409. Today this fails because
   `is_running` is False.
2. **Race.** Use a fake store whose `get_active_kubejobs` awaits an
   `asyncio.Event` and then returns `[]`. Start `reconcile_kubejob_builds()` as
   a task, and yield so that it is parked on the read. Await a successful
   `_dispatch_build_job`, set the event, and await the tick. Assert
   `is_running(task_id) is True`. Today this fails, either because nothing adds
   the task or because the assignment at `:719` removes it.
3. **Bounded to one tick.** After test 2, run one more tick with rows `[]`.
   Assert `is_running(task_id) is False`. This proves the set does not drift.
4. **A failed dispatch does not mark the task.** Make `dispatch` raise. Assert
   that the exception propagates and that `is_running(task_id) is False`.
5. **`force` still overrides.** With the task active, assert that
   `_refuse_recovery_while_running(..., force=True)` returns without raising.
6. **Existing tests still pass, unchanged:** the wholesale-replacement and
   store-failure tests in the same file
   (`test_finished_kubejob_stops_reading_as_running`,
   `test_store_failure_leaves_the_previous_answer_standing`).

Commands, run in the worktree:

```
apps/backend/.venv/bin/pytest tests/test_is_running_kubejob.py -v
ruff check apps/web-server/server/services/agent_kubejob.py apps/web-server/server/services/agent_service.py
```

Runtime check after deploy: dispatch a build, then call
`POST /api/tasks/{id}/recover` within 15 seconds. Expect a 409, where today the
task is reset.
