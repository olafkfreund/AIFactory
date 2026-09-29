---
status: draft
issue: 1628
spec: spec/2026-09-29-1628-running-row-survives-dispatch.md
---

# Plan: a row carrying a live Job reference is not terminal

## Approved decisions (self-contained)

- **Why.** A `/start`-dispatched build's job-state row is marked `done` at
  dispatch while its Job runs — measured at 15:08:00, 15:08:31, 15:09:01 and
  still at 15:16 with `Job active=1`, `ended_at` 1 ms after `updated_at`.
  `get_active_kubejobs` selects `lifecycle_state == "running"`, so reconcile,
  the reaper, the #1249 review re-drive, streamer cancellation, credential
  release and admission accounting are all blind to that build.
- **Cause.** `admit()` writes `running`; `/start` with no plan runs spec creation
  as an in-pod subprocess; that subprocess's exit handler
  (`agent_service.py:334`) marks the task terminal, guarded only by
  `task_id in self.running_tasks`, which an exited subprocess fails; the build
  then dispatches its Job and `set_worker_ref` records the k8s-job ref **without
  touching lifecycle**.
- **Fix, both ends of the race.** `set_worker_ref` returns the row to `running`
  and clears `ended_at` when the ref's kind is `k8s-job` — recording that a Job
  executes this task *is* the statement that it runs. The exit handler skips its
  terminal write when the row already carries a `k8s-job` ref. Either alone
  leaves a window.
- **`_KIND_PENDING` is not covered**: that stamp means a granted slot nobody has
  claimed (#1606), which is not running.
- **`mark_running` is not reused**: it stamps `worker_ref={"kind":"subprocess"}`
  by design and would erase the Job reference the reaper needs.
- **Scope is the `/start` path.** The contract-handoff path already holds a
  correct `running` row — the one "k8s Job reported succeeded" line since the
  control plane started is the 022 build's, across its full 145 minutes.
- **Never leave a dead build running**: the new write fires only when a ref is
  recorded, at dispatch, once. Terminal states stay owned by the reconcile loop.

## Steps

Branch `fix/1628-running-row-survives-dispatch` off `dev`. Two files, so under
the model-split rule this is implemented in-session rather than handed to a
coder agent.

1. **`job_state_store.set_worker_ref`**: when `worker_ref.get("kind")` is
   `k8s-job`, also set `lifecycle_state = "running"` and `ended_at = None`.
   → verify: a `done` row becomes `running` with `ended_at` cleared and the ref
   intact; a `_KIND_PENDING` ref leaves lifecycle untouched.
   **Mutation:** drop the lifecycle write — the first test must fail.
2. **The subprocess-exit terminal write** (`agent_service.py:334`): skip when the
   row's `worker_ref.kind` is `k8s-job`, logging why at debug.
   → verify: no terminal write for a k8s-job row; still written for a
   `subprocess` ref and for a row with no ref.
   **Mutation:** remove the guard — the first case must fail.
3. **`get_active_kubejobs` sees the repaired row**: one test that drives step 1
   then the query, so the fix is checked at the level that failed rather than
   only at the setter.
4. **Gates:** ruff and `ruff format --check` with CI's pinned 0.14.10, the full
   `tests/` suite, **and the co-located `apps/backend/test_*.py` root** — CI runs
   it separately and a change of mine passed `tests/` while failing it today.
5. **Live proof on a real `/start` build**: dispatch a task with no plan; while
   its Job is `active`, its row reads `running` and `get_active_kubejobs()`
   includes it; when the Job ends the control plane logs
   `"build <id> done (k8s Job reported succeeded)"` — which never appears for
   this path today — and the row is terminal with `ended_at` set and gone from
   the active list.
6. **PR → `dev`** with that evidence; close #1628.

## Tests

```sh
V=apps/backend/.venv/bin
$V/python -m pytest tests/ -q -k "job_state or worker_ref or kubejob"
$V/python -m pytest tests/ -q                      # the tests/ root
(cd apps/backend && $V/python -m pytest . -q)      # the co-located root CI runs
$V/ruff check apps/backend apps/web-server tests scripts
$V/ruff format --check apps/backend apps/web-server tests scripts
```

Expected: each new test fails before its step and passes after; both mutations
fail; the live build's row stays `running` for its duration and goes terminal
exactly once, at the end.

## Rollback

Revert the PR. `/start`-dispatched builds go back to being invisible to the
reconcile loop — no worse than today, and no stored state needs unwinding: the
change only affects which lifecycle value new writes record. Rows repaired while
it was live are already correct and are marked terminal by the normal path.
