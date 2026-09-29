---
status: draft
issue: 1628
intent: intent/2026-09-29-1628-running-row-survives-dispatch.md
---

# Spec: a row carrying a live Job reference is not terminal

## What the measurements settled

| Question | Measured answer |
| --- | --- |
| Is the row wrong for the whole build, or briefly? | **The whole build.** Job `active=1` and `lifecycle_state="done"` at 15:08:00, 15:08:31, 15:09:01 — and still both at 15:16, ten minutes in. It never self-corrects, because the only thing that would revisit it is the loop that ignores it. |
| Which write marks it terminal? | The **subprocess exit handler** (`agent_service.py:334`), whose only guard is `task_id in self.running_tasks` — false for a subprocess that has just exited. `ended_at` is 1 ms after `updated_at`, at the same second the Job starts. |
| Why does dispatch not correct it? | `set_worker_ref` (`job_state_store.py:391`) records `{"kind": "k8s-job", …}` and touches **nothing else** — so the row carries a live Job ref *and* `done`. |
| Who sets `running` normally? | `admit()` (`job_state_store.py:250`) at slot grant. `mark_running` exists but has **no callers**. |
| How wide is it? | The `/start` spec-creation → build path only. Exactly one "k8s Job reported succeeded" line exists since the control plane started — for the 022 contract-handoff build, which held `running` for its full 145 minutes. |

## Design

### The invariant, written where dispatch already writes

`set_worker_ref` gains the rule that gives it meaning: **a row that carries a
live k8s-job reference is running.** When the ref's `kind` is `k8s-job` it also
sets `lifecycle_state = "running"` and clears `ended_at`.

That is the whole fix. Recording "this Kubernetes Job is now executing this
task" *is* the statement that the task is running; leaving the two facts in
separate writes is what let them disagree. It is one place, on a call the
dispatch already makes, and it corrects the row no matter which order the
subprocess exit and the dispatch happen in — the property the guard-only
alternative lacks.

`_KIND_PENDING` refs are deliberately **not** covered: that stamp means a slot
was granted and nobody has claimed it (#1606), which is not the same as running.

### The exit handler also stops speaking for a kubejob build

`agent_service.py:334` marks the task terminal when a subprocess exits. For a
`/start` build that subprocess is *spec creation*, not the build, so it is
reporting the end of a phase as the end of the task. It now skips the write when
the row's `worker_ref.kind` is `k8s-job` — a Job owns that task's lifecycle, and
the reconcile loop is what will mark it terminal.

Both are implemented, and the intent's question 2 is answered **yes**: the two
writes are the two ends of the same race. `set_worker_ref` fixes a row already
marked done; the exit guard stops it being marked in the first place. Either
alone leaves a window; the rule is stated once in prose in both places, pointing
at this issue.

## Alternatives rejected

- **Guard the exit handler only.** Leaves the ordering load-bearing: if the exit
  lands after `set_worker_ref`, the row is still wrongly marked done.
- **`set_worker_ref` only.** Correct for the observed ordering, but the row is
  briefly terminal, and anything reading it in that window (admission, the
  cockpit) sees a finished build.
- **Call `mark_running` from the kubejob dispatch.** It stamps
  `worker_ref={"kind": "subprocess"}` by design — using it here would erase the
  Job reference the reaper needs, trading this bug for a worse one.
- **A row per phase.** The structural fix for two phases sharing one id, and a
  much larger change touching admission accounting. Worth revisiting if this
  recurs in another form; not the response to a known one-line race.
- **Have the cockpit ask Kubernetes instead.** Fixes one reader and leaves the
  reconcile loop, the reaper, the review re-drive and the credential pool still
  blind.

## Risks

- **Never leave a dead build running.** The intent's hard constraint. The new
  write only fires when a Job ref is recorded — at dispatch, once — so it cannot
  resurrect a finished row later. The reconcile loop still owns terminal states,
  and its `_done`/`_fail` writes are unchanged.
- **A dispatch that fails after `set_worker_ref`** would leave a `running` row
  with a Job that never ran. This is exactly what `reap_kubejob_builds` exists
  for (#1606 made it rebuild the deterministic Job name and ask the API), and
  restoring `running` is what lets the reaper see it at all — today such a row
  is invisible and leaks silently.
- **The exit guard could mask a genuine subprocess terminal state** if a row
  ever carried a k8s-job ref while a subprocess owned the build. Nothing writes
  both today; the guard keys on the ref rather than on the backend flag so it
  stays true if that changes.
- **Admission accounting shifts**: builds that currently vanish from the running
  set at dispatch will now occupy their slot for their real duration. That is
  the correct behaviour and may reduce apparent concurrency — which was
  over-counted precisely because these builds looked finished.

## Verification

Deterministic:

1. `set_worker_ref` with a `k8s-job` ref on a `done` row leaves it `running`
   with `ended_at` cleared, and the ref intact. **Mutation:** drop the lifecycle
   write and the test fails.
2. `set_worker_ref` with a `_KIND_PENDING` ref does **not** flip a row to
   running.
3. The subprocess exit handler skips its terminal write when the row's ref is
   `k8s-job`, and still writes it for a `subprocess` ref and for no ref at all.
   **Mutation:** remove the guard and the first case fails.
4. `get_active_kubejobs` returns a row in exactly the state step 1 leaves it.

Live, on a real `/start`-dispatched build — the shape that produced this issue,
and the level at which the unit story already looked fine:

5. Dispatch a task with no plan via `POST /start`. While its Job is `active`,
   its row reads `lifecycle_state = "running"` and `get_active_kubejobs()`
   includes it. Today both are false for the entire build.
6. The control plane logs `"build <id> done (k8s Job reported succeeded)"` when
   that Job finishes — proof the reconcile loop saw it, which it never does
   today for this path.
7. After completion the row is terminal, `ended_at` is set, and
   `get_active_kubejobs()` no longer lists it — the "never leave a dead build
   running" half of the constraint.

Gates: ruff and `ruff format --check` with CI's pinned 0.14.10, the full backend
suite, **and the co-located `apps/backend/test_*.py` root** — CI runs it as a
separate step and a change of mine passed `tests/` while failing it today.
