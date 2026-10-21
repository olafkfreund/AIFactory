---
status: approved
issue: 1606
author: Olaf Krasicki-Freund
---

# Intent: a crashed dispatch must not hold a concurrency slot forever

## Problem

A build is admitted against the global concurrency cap, its Kubernetes Job is
created, and only then is the worker reference written to the durable row:

1. `apps/web-server/server/services/build_backend.py:1284` — `create_namespaced_job(...)`
2. `apps/web-server/server/services/build_backend.py:1292` — `set_worker_ref(...)`

A hard process crash between those two statements leaves a `job_states` row in
`lifecycle_state='running'` with `worker_ref = NULL`. The row is admitted, so it
counts against the cap; it has no Kubernetes reference, so nothing can find the
Job it belongs to.

Every cleanup path then declines to touch it:

- `job_state_store.py:413-449` — `get_active_kubejobs()` skips any row whose
  `worker_ref.kind != "k8s-job"`, so a NULL ref is never even returned.
- `build_backend.py:1394-1400` — skips rows with no `job_name`, commented
  "leave for the deadline path". That path (`:1422-1436`) sits under
  `if outcome == "running":` and cannot be reached without a `job_name`. The
  comment describes a path that does not exist.
- `agent_kubejob.py:830-838` — `_has_live_kubejob()` treats any `running` row as
  proof of a live build, so the stuck row shields its own task from
  `reap_abandoned_tasks`.
- `stale_reaper.py:55-71`, `stale_tasks.py` — reason about spec-directory
  mtimes, never about `job_states`, and default to disabled/report-only.
- `agent_service.py:1465-1498` — startup reconciliation rebuilds state and
  drains the queue, but does not look for ref-less rows.
- `agent_service.py:847-876` — marks the row terminal when `_start_build_unit()`
  *raises*. This covers the exception path and only that path; it cannot run at
  all if the process died.

The effect is a permanent, silent reduction in capacity. `MAX_CONCURRENT_TASKS`
defaults to 5 (`apps/web-server/server/config.py:220`), so five such events over
the lifetime of a deployment wedge the control plane completely, with no log
line and no alert, recoverable only by hand-editing Postgres.

The leak has not fired yet — the table currently holds zero `running` and zero
`queued` rows — which is precisely why it is worth fixing before it does.

## Proposed outcome

A `running` row with no usable worker reference is reconciled by the same
machinery that reconciles every other running row, instead of being skipped by
all of them. After a control-plane crash mid-dispatch, the slot returns to the
pool without human intervention, and the event is visible in the logs rather
than silent.

Concretely, observable:

- Killing the control plane between Job creation and `set_worker_ref` leaves a
  row that a subsequent reconciliation pass moves out of `running`.
- The global cap admits new work again afterwards.
- The transition is logged, so the same failure is diagnosable next time.

## Affected users and systems

- `apps/web-server/server/services/build_backend.py`, `job_state_store.py`,
  and whichever reconciliation entry point ends up owning ref-less rows.
- The deployed AIFactory control plane in the `factory` namespace. No change to
  the build Job itself, the gate path, or any agent prompt.
- Anyone whose tasks queue behind a cap that has silently shrunk.

## Constraints

- Must not free a slot whose Job is genuinely still running. A row with no
  `worker_ref` is ambiguous on its face: the Job may exist in Kubernetes even
  though the reference was never persisted. Reconciliation has to establish
  which case it is — by label lookup against the API — not assume the Job is
  absent and orphan a live build.
- Must not widen `get_active_kubejobs()` in a way that makes its other callers
  dereference a NULL ref.
- Must keep the cap's accounting durable and transactional; admission already
  runs `SELECT … FOR UPDATE` in `job_state_store.admit()`.
- Fix the root cause once, in the shared path, rather than adding a guard to
  each caller that currently skips the row.
- Test coverage is required: the acceptance criterion of #1425 depends on this
  behaviour existing, and the failure is invisible without a test.

## Open questions

1. **Ordering vs reconciliation.** Writing `worker_ref` *before* creating the
   Job (or in the same transaction) would shrink the window rather than handle
   it — but it cannot close it, since the crash could then land between the ref
   write and a Job creation that never happens, leaving a ref pointing at
   nothing. My reading is that reconciliation is the real fix and the ordering
   change is optional hardening; I would like that confirmed rather than
   assumed.
2. **Where reconciliation belongs.** Widening `get_active_kubejobs()` to return
   ref-less rows and letting the existing deadline branch handle them is the
   smallest diff. Adding a dedicated startup/periodic sweep is more explicit but
   duplicates logic. I lean to the former; approver's call.
3. **Whether a ref-less row should be resolvable by label.** Build Jobs carry
   labels; if they identify the task, reconciliation can ask Kubernetes whether
   the Job exists instead of guessing. Worth confirming the labels are
   sufficient before the spec commits to it.
