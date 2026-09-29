---
status: draft
issue: 1628
author: olafkfreund
---

# Intent: a running build's row says it is running

## Problem

A build dispatched through `POST /start` has its job-state row marked **terminal
at dispatch**, while its Kubernetes Job runs. Measured on task
`888c19de…:023-1619-liveness-probe-readme-not`, sampled three times over a
minute:

```
15:06:04Z     k8s Job factory-aifactory-ess-probe-readme-not  startTime
15:06:04.202  row updated_at
15:06:04.203  row ended_at              <- 1 ms later
15:08:00 / 15:08:31 / 15:09:01   Job .status.active = 1
                                 row lifecycle_state = "done", error = None

store.get_active_kubejobs() -> []
kubernetes active Jobs      -> ["factory-aifactory-ess-probe-readme-not"]
```

**Root cause, traced through the call path:**

1. `admit()` writes the row with `lifecycle_state = "running"` when the slot is
   granted (`job_state_store.py:250`).
2. `/start` on a task with no plan runs **spec creation as an in-pod
   subprocess**.
3. When that subprocess exits, the exit handler marks the task's row terminal
   (`agent_service.py:334`) — it maps the control-plane status and writes
   `done`. Its only guard is `if task_id in self.running_tasks`, which a
   *subprocess* that has just exited no longer satisfies.
4. The build phase then dispatches the Kubernetes Job and calls
   `set_worker_ref` (`job_state_store.py:391`), which records
   `{"kind": "k8s-job", …}` and **does not touch `lifecycle_state`**.

So the row ends up carrying a live Job reference *and* a terminal state. One row
per task id, two phases, and the first phase's exit speaks for the whole task.

**What that breaks.** `get_active_kubejobs` selects `lifecycle_state ==
"running"`, so everything keyed on it is dead for such a build:

- `reconcile_kubejob_builds` never polls it — a missed completion strands the
  build, which is the failure #671 added the loop to prevent;
- `reap_kubejob_builds` never notices a vanished Job;
- `_redrive_kubejob_review` never runs (#1249 rides this loop);
- the log streamer is never cancelled and the pooled credential never released;
- admission/concurrency accounting believes nothing is running;
- and #1619's cockpit fix, which consults this same store, still answers
  "not running" for these builds.

**Scope is measured, not assumed.** `_done` logs "k8s Job reported succeeded"
only for a row the store returned as running. Since the control plane started
there is exactly one such line, for the **022** build dispatched by the PFactory
contract handoff — which held a correct `running` row for its full 145 minutes.
So the handoff path is sound and this is the `/start` spec-creation → build
transition specifically.

## Proposed outcome

While a build's Kubernetes Job is executing, its row says `running`, so the
reconcile loop, the reaper, the review re-drive, credential release and the
cockpit all see it. A build that has genuinely finished still goes terminal
exactly as it does today.

## Affected users and systems

- `apps/web-server/server/services/job_state_store.py` — `set_worker_ref`, and
  the invariant that a row carrying a live k8s-job ref is not terminal.
- `apps/web-server/server/services/agent_service.py` — the subprocess exit
  handler that currently speaks for the whole task.
- Everything downstream of `get_active_kubejobs`: reconcile, reaper, review
  re-drive, streamer cancellation, credential pool, admission accounting.
- Not the contract-handoff path, which is already correct and must stay so.

## Constraints

- **Never mark a live build terminal** — the defect — and **never leave a dead
  build running**, which strands its slot and makes `/start` refuse with
  "already running". A fix that trades one for the other is not a fix.
- The subprocess exit handler must keep working for genuine subprocess builds;
  this is not a licence to stop recording terminal states.
- Prove it on a **real `/start`-dispatched build**, watching the row while the
  Job runs and after it ends. The unit-level story looked correct here too —
  that is why the defect survived.
- No change to the 022-style handoff path's observed behaviour.

## Open questions

1. **Where does the invariant belong?** My recommendation: `set_worker_ref`,
   when it records a `k8s-job` ref, also returns the row to `running` and clears
   `ended_at` — recording that a Job is now executing this task *is* the
   statement that it is running, and it is one place, on the call the dispatch
   already makes. The alternative is to guard the exit handler so it does not
   speak for a task whose row carries a k8s-job ref; that is also correct but
   leaves the ordering fragile if the ref is written after the exit.
2. **Should both be done?** Belt and braces costs little and closes the race
   from either direction, at the price of two places expressing one rule.
3. **Is a phase-scoped row the real answer?** One row per task id is what makes
   two phases collide. Splitting rows per phase is a larger change and would
   touch admission accounting; I would not do it here, but it is the structural
   fix if this recurs.
