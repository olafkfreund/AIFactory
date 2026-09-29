---
status: approved
issue: 1619
spec: spec/2026-09-29-1619-cockpit-truthful-builds.md
---

# Plan: the cockpit tells the truth about a running build

## Approved decisions (self-contained)

- **Why.** A healthy kubejob build reads as **Stuck / Interrupted** with a
  **Recover** button, a frozen `planning` phase, grey progress dots, an empty
  console and an age an hour off. Measured on task
  `888c19de…:022-myfriends-shared-core-remediat`: the Job ran 145 minutes and
  produced 42 commits and 11,436 insertions while `GET /running` answered
  `false` and `task_logs.json` held 0 entries throughout.
- **`is_running` is a dict lookup.** `agent_service.py:1500` returns
  `task_id in self.running_tasks`, and the only write (line 1196) is directly
  after `asyncio.create_subprocess_exec` — the in-pod subprocess backend. A
  kubejob build never passes through it. Fix: check the dict **or** a set of
  active kubejob task ids maintained by `reconcile_kubejob_builds()`, which
  already polls those rows every 15s and whose `row["job_id"]` **is** the task id.
  The set is **replaced wholesale** each tick, never mutated incrementally: a
  set that only grows would start claiming dead builds are alive, which is the
  worse failure.
- **The progress path exists and is broken, not missing.** #1110 already parses
  the Job's `[PHASE_EVENT]` lines into `task_logs.json`, which
  `get_execution_progress` reads. `_await_pod_name` waits for the **pod object**
  (seconds after dispatch) rather than the `task` container (86s later, after
  three init containers), and `stream()` treats the resulting immediate EOF as
  success. Fix it in `build_log_stream.py` only. **The #1618 heartbeat design is
  withdrawn** — it would have added storage and a `run.py` writer to carry data
  the Job already emits.
- **`recover` refuses while a Job is live**, with explicit `force: true`,
  expressed in terms of the fixed `is_running` so the refusal and the card
  cannot disagree.
- **Timestamps become timezone-aware** on write; reads stay tolerant of the
  naive rows already stored, and no stored row is migrated.
- **The coder's spec/plan are copied into the subtask worktree** rather than
  referenced across the sandbox boundary. The doubling is in the system-set
  worktree path, not the model's typing, and whether the sandbox refuses an
  absolute read outside its root is unmeasured — the copy is correct either way.
- **Proof is live**, not unit-only: the defect is precisely that unit-level
  reasoning about `running_tasks` looked correct while the cockpit lied.

## Steps

Branch `fix/1619-cockpit-truthful-builds` off `dev`, one commit per step.

1. **`is_running` consults the kubejob set.** Add
   `self._active_kubejob_task_ids: set[str]` to `AgentService.__init__`; replace
   it wholesale at the end of `reconcile_kubejob_builds()` from the rows just
   polled, minus those that went terminal in that tick; widen `is_running`.
   → verify: unit tests for present-in-set, present-in-dict, present-in-neither,
   and two consecutive ticks with different rows dropping the stale id.
   **Mutation:** remove the set from the expression; the first test must fail.
2. **`recover` refuses while a Job is live.** Add `force: bool = False` to
   `RecoverTaskRequest`; 409 when `is_running(task_id)` and not `force`, naming
   the Job.
   → verify: 409 without force, proceeds with force. **Mutation:** force
   `is_running` False and the 409 test must fail.
3. **Streamer waits for the container.** `_await_pod_name` (or a sibling)
   returns only once the pod's `task` container is `running` or terminated.
   → verify: a fake pod whose container is still waiting is not streamed from
   until it starts; the existing timeout still bounds the wait.
4. **Streamer reattaches until the Job is terminal.** A clean EOF re-follows
   with backoff while the Job is active, passing `since_time`; the terminal log
   line becomes a **warning** when the Job is still running.
   → verify: a line source that ends after N lines is re-entered while the Job
   reads active and not once it reads terminal; the warning appears in the
   early-end case. **Mutation:** restore the "EOF means done" behaviour and the
   reattach test must fail.
5. **Timezone-aware timestamps.** `datetime.now()` → `datetime.now(UTC)` at
   `agent_service.py:1200` and every sibling task/subtask timestamp writer the
   same sweep finds; reads parse a naive value as UTC.
   → verify: a written value round-trips as aware UTC; a naive stored value
   still parses. Record the sweep's file list in the PR so "every sibling" is
   checkable rather than asserted.
6. **Copy spec and plan into the subtask worktree** at dispatch, and log a
   warning naming both paths when the coding phase cannot read them.
   → verify: both files exist inside the subtask worktree in a dispatched build;
   the warning fires when they are absent.
7. **Live proof, in-cluster** — dispatch a real build (a small spec, not the
   21-subtask demo) and measure, with the build untouched:
   - `GET /api/tasks/<id>/running` → `true` while the Job is `Running`;
   - the card shows no Stuck badge;
   - `POST /recover` → 409, and the Job is still `Running` afterwards;
   - `task_logs.json` entry count sampled twice, minutes apart, **increases**;
   - the task's `phase` leaves `planning` while the Job runs, and subtasks move
     off `pending`;
   - zero "File does not exist" failures for `spec.md` /
     `implementation_plan.json` in the coding log;
   - after the Job ends, `/running` returns `false` within one reconcile tick.
8. **Gates:** ruff, `ruff format --check` over the CI path list,
   `ratchet_lint.py --base origin/dev` with its `--package` flags, the full
   backend suite, each new test module collected alone, and the frontend
   typecheck + vitest only if a frontend file changed (none is expected).
9. **PR → `dev`** carrying the step 7 evidence; close #1619, #1618 and #1617.

## Tests

```sh
apps/backend/.venv/bin/python -m pytest tests/ -q -k "is_running or kubejob"
apps/backend/.venv/bin/python -m pytest tests/ -q -k "build_log_stream"
apps/backend/.venv/bin/python -m pytest tests/ -q -k "recover"
apps/backend/.venv/bin/ruff check apps/backend apps/web-server tests scripts
apps/backend/.venv/bin/ruff format --check <the repo's CI path list>
apps/backend/.venv/bin/python scripts/ratchet_lint.py --base origin/dev \
  --package apps/backend --package apps/web-server --package scripts
```

Expected: each new test fails before its step and passes after; all three
mutations fail; the live build reports running, refuses recovery, and grows its
log while it runs.

## Rollback

Revert the PR. `is_running` returns to the dict, so kubejob builds read as stuck
again; the streamer returns to giving up before the container starts; `recover`
stops refusing. No persistent state is written by any of these changes, and no
build is affected either way — every change is in how the control plane observes
and reports, never in how a build runs.
