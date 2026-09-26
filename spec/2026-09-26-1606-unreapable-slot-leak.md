---
status: draft
issue: 1606
intent: intent/2026-09-26-1606-unreapable-slot-leak.md
---

# Spec: reconcile running rows whose worker was never claimed

> **Revision 2.** Revision 1 was approved and then falsified by its own tests
> during implementation. It assumed the leaked row has `worker_ref = NULL`; it
> never does. The corrected premise and the consequent redesign are below. The
> intent is unaffected — the leak, its impact and its constraints all stand.

## Corrected premise

`admit()` stamps **every** granted slot with `worker_ref = {"kind": "subprocess"}`
before anyone knows which backend will run it:

- `job_state_store.py:252` — first admission (`if grant else None`)
- `job_state_store.py:265` — re-admission of a previously terminal id
- `job_state_store.py:328` — `drain()` promoting a queued row

`worker_ref` is NULL only on the **not-granted** branch, i.e. a `queued` row. So
a `running` row with no reference **cannot occur**, and revision 1's design was
dead code. Proven by its own test: `get_active_kubejobs()` returned `[]` for a
freshly admitted row because the ref said `subprocess`.

The kubejob backend overwrites the stamp later, at `build_backend.py:1292`, which
documents the sequence at `:1226`: *"the job-state row is `running` with
`worker_ref={kind:subprocess}` from `admit`; we overwrite worker_ref with the
k8s-job reference"*.

The real leaked state is therefore `lifecycle_state='running'` **with**
`worker_ref = {"kind": "subprocess"}` on a deployment that dispatches via
kubejob. Nothing reaps it: no code outside `job_state_store` consumes a
subprocess-kind row at all.

That state is **ambiguous on its face** — it means either "kubejob dispatch
crashed before `set_worker_ref`" or "a genuine subprocess build is running".
Reaping the latter kills a live build, which the intent forbids. The ambiguity,
not the missing reference, is the defect.

## Design

**Stop asserting a backend that has not been chosen.** `admit()` does not know
which backend will run the slot, so it must not claim one. The three admission
sites stamp `{"kind": "pending"}` instead of `{"kind": "subprocess"}`:

- `job_state_store.py:252`, `:265`, `:328` — `{"kind": "pending"} if grant else None`

`mark_running()` (`:284`) **keeps** writing `{"kind": "subprocess"}`: that is the
subprocess path genuinely declaring itself, and it is what makes the two states
distinguishable. After this change:

| `worker_ref.kind` | meaning |
| ----------------- | ------- |
| `pending`         | a slot was granted; no worker has claimed it yet |
| `subprocess`      | the subprocess path is running it (`mark_running`) |
| `k8s-job`         | dispatched as a Job (`set_worker_ref`) |

A `running` row still stamped `pending` after dispatch should have been claimed
and was not — unambiguously the crash window, with no live subprocess build to
confuse it with.

**The reaper then reconciles `pending` rows, as revision 1 described.**
`get_active_kubejobs` (`job_state_store.py:450`) returns rows whose kind is
`pending` (with `job_name`/`namespace` as `None`) as well as `k8s-job` rows, and
keeps skipping `subprocess`. `reap_vanished_jobs` (`build_backend.py:1379`)
rebuilds the reference and asks the API, exactly as approved in revision 1:

- **Name** from `job_dispatch.job_name(service, job_id)`, deterministic —
  `factory-<service>-<_short(job_id)>` (`job_dispatch.py:185-192`); builds
  dispatch with `job_id=task_id` (`build_backend.py:751,930`).
- **Namespace** as dispatch resolves it (`AIFACTORY_SANDBOX_NAMESPACE`, default
  `factory`).
- **Guarded by the label.** `_short()` keeps only the last 20 characters, so ids
  can collide. A resolved Job's `factory.io/job-id` label must match before any
  terminal write; the expected value comes from the public
  `job_labels("aifactory", job_id)` — the function that stamped it — so the check
  cannot drift from the labelling rule. Mismatch, missing label or API error →
  leave the row alone and log. Fail closed.
- **Repair.** A verified reconstruction is persisted via `set_worker_ref`, so
  later ticks take the ordinary path.
- **404 on a reconstructed name** is reaped with a reason distinguishing it from
  the recorded-ref case ("disappeared without a terminal write").

**Blast radius of the `pending` stamp, verified by enumeration.** Every reader of
`worker_ref["kind"]` tests `!= "k8s-job"` and bails; none tests
`== "subprocess"`, so none relies on the placeholder's current value:

| Reader | With `pending` |
| ------ | -------------- |
| `agent_kubejob.py:581` `_kubejob_worker_ref` | returns `None` — unchanged |
| `agent_kubejob.py:940` stop path | returns `False` — unchanged |
| `build_backend.py:1315` `delete_job` | returns `False` — unchanged |
| `job_state_store.py:450` | the one predicate that changes |

`models.py:1191`'s comment (`worker_ref{kind="subprocess"|"k8s-job"}`) is updated
to document the third value.

## Alternatives rejected

**Treat `subprocess`-stamped rows as kubejob candidates when
`AIFACTORY_BUILD_BACKEND=kubejob`.** Correct on the current cluster, where
kubejob is the live default and every build is one — but it is a
config-dependent heuristic that silently becomes wrong for any deployment
running both backends, and it leaves the ambiguity in the data model. Rejected
in favour of removing the ambiguity at its source.

**Write the k8s-job ref before creating the Job.** Revision 1 rejected
reordering because it moves the crash window rather than closing it, and that
still holds: a crash after the ref write and before Job creation leaves a
reference to a Job that does not exist. Under this design that row is reconciled
anyway — `_job_outcome` returns `gone` and the existing path fails it — so
reordering buys nothing and still touches the live dispatch path. Rejected
again, on the stronger ground that reconciliation now covers both sides.

**A dedicated sweep for `pending` rows.** Duplicates the decision tree
`reap_vanished_jobs` already implements and creates a second writer of terminal
state. Rejected (unchanged from revision 1).

**Leave `admit()` alone and reap ref-less rows only.** What revision 1 specified.
Dead code: the state cannot occur.

**Have `_has_live_kubejob()` stop trusting `running`** (`agent_kubejob.py:830-838`).
A symptom that disappears once these rows are reconciled. Out of scope.

## Risks

- **Freeing a slot whose build is alive.** Still the failure mode that matters.
  Now guarded twice: `pending` cannot collide with a running subprocess build,
  and the label check must pass before any terminal write.
- **A row stamped `pending` that a backend claims moments later.** The reaper
  asks the API, finds the Job (if dispatched) and repairs the ref, or finds
  nothing and reaps a row that genuinely has no worker. The deadline guard
  already governs how long a row may sit unclaimed.
- **Rows written before this change** carry `subprocess` and stay invisible to
  the reaper. Accepted: there are none in the live table today (0 `running`,
  0 `queued`), and inventing a migration for a state that does not exist is
  more risk than it removes. Called out in the plan as a known limitation.
- **`_short()` name collision** — the reason the label check exists, and still
  the most important test case.
- **Blast radius.** Control plane only. No change to build Jobs, gates, agent
  prompts or dispatch. No schema change: `worker_ref` is a nullable JSON column
  (`models.py:1192`), so a new `kind` value needs no migration.
- **cq-ratchet.** Both halves must report "0 regressed", and it diffs committed
  history, so it runs after the commit.

## Verification

1. **Unit — the premise, pinned.** A freshly admitted row is `running` with
   `worker_ref.kind == "pending"`, and `mark_running()` moves it to
   `"subprocess"`. This is the assertion whose absence let revision 1 be wrong.
2. **Unit — the query.** A `pending` row is returned with `job_name`/`namespace`
   as `None`; a `subprocess` row is not; a `k8s-job` row is, as before.
3. **Unit — the regression.** A `pending` row whose Job is live: the reaper asks
   the API and leaves the row `running`. Fails on `origin/dev`, where the row is
   never returned.
4. **Unit — all four outcomes** for a `pending` row: `succeeded → done`,
   `failed → failed`, `running → untouched`, `absent → failed with a reason`.
5. **Unit — the collision guard.** Reconstructed name matches, label does not:
   row untouched, mismatch logged. Asserts on the log, since silence is the bug
   class.
6. **Unit — the slot returns.** After reconciliation, `admit()` grants a slot it
   previously refused at the same cap.
7. **Unit — ref repair.** A verified reconstruction is written back and a second
   tick takes the recorded-ref path.
8. **Existing suite.** The kubejob, job-state and reaper suites must pass
   unchanged — they are the regression net for the `pending` stamp.
9. **Ratchet.** Both halves, after committing, "0 regressed".
10. **Not verified live.** Reproducing the crash means killing the control plane
    mid-dispatch. Out of scope; #1425 must not be closed on these tests alone.
