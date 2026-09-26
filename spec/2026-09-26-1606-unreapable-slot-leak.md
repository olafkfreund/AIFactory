---
status: draft
issue: 1606
intent: intent/2026-09-26-1606-unreapable-slot-leak.md
---

# Spec: reconcile running rows that have no worker reference

## Design

The reaper is not missing logic. `BuildBackend.reap_vanished_jobs`
(`apps/web-server/server/services/build_backend.py:1379`) already holds the
complete decision tree for a `running` row, keyed on what Kubernetes says:

| `_job_outcome` | action |
| -------------- | ------ |
| `succeeded`    | `_done(job_id)` (#857 — take the kubelet's answer) |
| `failed`       | `_fail(...)` |
| `running`      | deadline guard, else leave alone |
| Job absent     | `_fail(...)` "disappeared without a terminal write" |

A ref-less row never reaches that tree, for two independent reasons, and the fix
is to remove both rather than to add a new code path:

**1. The row is filtered out before the reaper sees it.**
`JobStateStore.get_active_kubejobs` (`job_state_store.py:437`) drops any row
whose `worker_ref.kind != "k8s-job"`, so a NULL ref never appears in `rows`.

Change: keep the filter's intent — a row whose worker is some *other* kind of
worker is still none of this reaper's business — but stop conflating "a
different kind of worker" with "no reference recorded yet". A row with an empty
or absent `worker_ref` is a k8s-job row whose reference was lost, and it is
returned with `job_name`/`namespace` as `None`.

**2. The reaper bails on a row with no name.**
`build_backend.py:1396-1400` does `if not job_name or not namespace: continue`,
commented "leave for the deadline path" — a path that cannot be reached without
a `job_name` (it is nested under `if outcome == "running":`). That comment is
removed along with the `continue`.

Change: when the row carries no reference, **reconstruct** it rather than give
up. Both halves are derivable:

- **Name.** `job_dispatch.job_name(service, job_id)` is deterministic —
  `factory-<service>-<_short(job_id)>` (`job_dispatch.py:185-192`) — and a build
  dispatches with `job_id=task_id` (`build_backend.py:751,930`). The same inputs
  that named the Job at dispatch are still on the row.
- **Namespace.** The namespace the backend dispatches into, i.e. the same value
  `_build_manifest` uses; not re-derived from the row.

The reconstructed reference is then passed through the *unchanged* `_job_outcome`
call, so all four outcomes behave exactly as they do for a row that kept its
reference. This is what satisfies the intent's binding constraint: a ref-less
row is ambiguous, and we resolve the ambiguity by **asking the Kubernetes API**,
never by assuming the Job is absent.

**Guarding the reconstruction.** `_short()` keeps only the last 20 characters of
the id (`job_dispatch.py:185-188`), so two distinct `job_id`s can produce the
same Job name. A reconstructed hit is therefore verified against the Job's
`factory.io/job-id` label (`job_labels()`, `job_dispatch.py:286-296`) before any
terminal write. On mismatch the row is left alone and the collision is logged:
a missed reap is recoverable, failing the wrong build is not.

A reconstructed reference that resolves to a live Job is persisted back to the
row via the existing `set_worker_ref`, so the repair happens once and subsequent
ticks take the ordinary path.

Every transition is logged with the reason, which the intent requires — the
present failure is silent, and that is most of what makes it bad.

## Alternatives rejected

**Write `worker_ref` before creating the Job, or in one transaction.** Shrinks
the window without closing it: the crash simply moves to the other side of the
ref write, leaving a reference to a Job that was never created. It also touches
the live dispatch path, where a mistake costs more than the bug being fixed.
Reconciliation is strictly more correct because it consults Kubernetes. Answers
the intent's open question 1: reordering is not needed, and is not included.

**A dedicated startup or periodic sweep for ref-less rows.** Duplicates the
decision tree that `reap_vanished_jobs` already implements, and a second writer
of terminal state invites divergence between the two. Answers the intent's open
question 2 in favour of reusing the existing reaper.

**Widen `get_active_kubejobs()` and let the existing `continue` stand.** Feeds
the reaper rows it will silently skip — motion without effect. Both halves must
change or neither is worth changing.

**Treat a ref-less row as dead and free the slot.** The smallest possible diff,
and wrong: it orphans a genuinely running build whose ref write was the only
thing that failed, turning a capacity bug into a correctness bug. Explicitly
excluded by the intent's constraints.

**Have `_has_live_kubejob()` stop trusting `running`.** `agent_kubejob.py:830-838`
is a symptom, not the cause — once ref-less rows are reconciled, a stale
`running` row no longer persists for it to trust. Changing it as well would
affect every caller's notion of liveness for no additional benefit.

## Risks

- **Freeing a slot whose build is alive.** The failure mode that matters. Guarded
  by asking Kubernetes and by the label cross-check; a mismatch or an API error
  leaves the row untouched.
- **`_short()` name collision.** Two ids sharing a 20-character suffix could
  make a reconstructed name resolve to another task's Job. This is why the label
  check exists, and it is the single most important test case.
- **Widening `get_active_kubejobs()` affects its other callers.** They must
  tolerate `job_name`/`namespace` of `None`. Callers are enumerated during
  implementation; any that dereference the reference unconditionally are fixed
  or excluded in the same change.
- **A transient Kubernetes API error looks like "Job absent".** `_job_outcome`'s
  existing error handling is reviewed rather than trusted; "absent" must mean a
  definite 404, not any failure to ask.
- **Blast radius.** Control plane only, in the `factory` namespace. No change to
  build Jobs, gates, agent prompts, or the dispatch path.
- **cq-ratchet.** Both halves must pass the strict ruff + mypy baseline, and the
  ratchet diffs committed history, so it is run after committing.

## Verification

1. **Unit — the leak, reproduced.** A `running` row with `worker_ref = NULL` and
   a Job present in a fake batch API: `reap_vanished_jobs` reconciles it instead
   of skipping it. This test fails on `origin/dev` today, which is what makes it
   the regression test.
2. **Unit — all four outcomes** for a ref-less row: `succeeded → done`,
   `failed → failed`, `running → untouched`, `absent → failed with a reason`.
3. **Unit — the collision guard.** A Job whose reconstructed name matches but
   whose `factory.io/job-id` label does not: the row is left alone and the
   mismatch logged. Asserts on the log, since silence is the bug class.
4. **Unit — the slot is actually returned.** After reconciliation,
   `job_state_store.admit()` admits work that the cap previously refused. This
   ties the fix to the observable outcome the intent promises, rather than to
   the row's state alone.
5. **Unit — ref persistence.** A reconstructed reference that resolves to a live
   Job is written back, and a second tick takes the ordinary path.
6. **Existing suite.** `apps/backend/.venv/bin/pytest tests/ -v` for the reaper
   and job-state modules, to confirm the widened query broke no caller.
7. **Ratchet.** Both halves of cq-ratchet, run after committing, reporting
   "0 regressed".
8. **Not verified live.** Reproducing this in the cluster means killing the
   control plane mid-dispatch. Out of scope here; the fake-API tests cover the
   logic, and #1425 should not be closed on the strength of this alone.
