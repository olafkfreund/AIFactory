---
status: draft
issue: 1606
spec: spec/2026-09-26-1606-unreapable-slot-leak.md
---

# Plan: reconcile running rows that have no worker reference

Self-contained summary of the approved decisions.

## Approved decisions (carried from the spec)

- **The bug.** `build_backend.py:1284` creates the Job, `:1292` writes
  `worker_ref`. A crash between them leaves a `job_states` row `running` with
  `worker_ref = NULL`, holding a concurrency slot that nothing frees.
- **Reuse the existing reaper.** `BuildBackend.reap_vanished_jobs`
  (`build_backend.py:1379`) already handles all four outcomes
  (`succeeded → _done`, `failed → _fail`, `running → deadline guard`,
  `gone → _fail`). It is starved of rows, not missing logic. No new sweep, no
  second writer of terminal state.
- **Do not reorder the dispatch writes.** Reordering moves the crash window
  rather than closing it and touches the live dispatch path. Excluded.
- **Resolve ambiguity by asking Kubernetes.** A ref-less row may still have a
  live Job. Never assume absence.
- **Reconstruct the reference, do not guess it.**
  `job_dispatch.job_name(service, job_id)` is deterministic —
  `factory-<service>-<_short(job_id)>` (`job_dispatch.py:185-192`) — and builds
  dispatch with `job_id=task_id` (`build_backend.py:751,930`).
- **Guard the reconstruction with the label.** `_short()` keeps only the last 20
  characters (`job_dispatch.py:185-188`), so distinct ids can collide. Verify a
  resolved Job's `factory.io/job-id` label (`job_labels()`, `:286-296`) before
  any terminal write; on mismatch leave the row alone and log. A missed reap is
  recoverable, failing the wrong build is not.
- **Persist a repaired reference** via the existing `set_worker_ref`, so later
  ticks take the ordinary path.
- **Log every transition with its reason.** The present failure is silent, which
  is most of what makes it bad.

## Findings that settled the spec's deferred items

- **Only one other caller, and it is safe.** `get_active_kubejobs()` is called
  from `build_backend.py:1386` and `agent_kubejob.py:638`. The latter uses
  `row["job_id"]` only, passing it to `reconcile_by_poll`, which reads durable
  state and never touches `job_name`/`namespace`. No change needed there.
- **The 404 requirement is already met.** `_job_outcome` returns `gone` only on
  a literal `status == 404`; every other exception logs and returns `running`
  ("fail safe, never reap a build we could not verify"). Inherited, not added.
- **New nuance to encode.** A 404 on a *reconstructed* name is weaker evidence
  than on a recorded one: it is consistent with both "Job never created" (the
  leak — reap it) and "created under a colliding name". The label check cannot
  run, because there is no Job to read. Still reapable, but logged distinctly so
  the two are separable afterwards.

## Steps

1. `apps/web-server/server/services/job_state_store.py:437` — in
   `get_active_kubejobs`, stop conflating "a different kind of worker" with "no
   reference recorded". Keep skipping a row whose `worker_ref.kind` is present
   and is not `k8s-job`; return a row whose `worker_ref` is empty/absent, with
   `job_name` and `namespace` as `None`. Docstring updated to say the query now
   returns ref-less rows and why. → verify by a unit test asserting a NULL-ref
   `running` row appears in the result with both fields `None`, and that a row
   with `kind: "subprocess"` still does not.

2. `apps/web-server/server/services/build_backend.py:1396-1400` — replace
   `if not job_name or not namespace: continue` (and its comment, which points
   at unreachable code) with reconstruction: when either is missing, derive the
   name via `job_dispatch.job_name(service, job_id)` and use the namespace the
   backend dispatches into. Mark the row as reconstructed so steps 3 and 4 can
   branch on it. Fall through into the *unchanged* `_job_outcome` call. → verify
   by a unit test where a ref-less row with a present Job reaches
   `_job_outcome` at all (fails on `origin/dev` today).

3. `build_backend.py` (same block) — before any terminal write on a
   reconstructed row, read the Job's `factory.io/job-id` label and compare it to
   `_short(job_id)`. On mismatch: leave the row untouched, log a warning naming
   both ids, and move on. → verify by a unit test with a colliding Job name and
   a non-matching label, asserting the row is unchanged and the warning logged.

4. `build_backend.py` (same block) — on a reconstructed reference that resolves
   to a live Job whose label matches, write it back with `set_worker_ref`, and
   log the repair. On a 404, proceed to the existing `_fail` path but with a
   reason distinguishing "no Job under the reconstructed name" from the recorded
   "disappeared without a terminal write". → verify by a unit test asserting
   `set_worker_ref` was called with the reconstructed values, and a second tick
   takes the recorded-ref path.

5. `tests/` — add the cases from the spec's verification list as one new test
   module beside the existing reaper tests, using the fake batch API already
   used by `tests/test_kubejob_review_redrive.py:99`. Include the slot-return
   assertion: after reconciliation, `job_state_store.admit()` admits work the
   cap previously refused. → verify by the commands below.

6. Commit, then run both halves of cq-ratchet (it diffs committed history, so it
   must run after the commit, not before). → verify by "0 regressed" from each
   half.

7. Open the PR against `dev`, linking intent, spec and plan. → verify by the PR
   body containing all three paths and `Closes #1606`.

## Tests

```bash
# the new module (expected: all pass; step 2's case fails on origin/dev)
apps/backend/.venv/bin/pytest tests/test_reap_refless_rows.py -v

# no regression in the reaper / job-state suites
apps/backend/.venv/bin/pytest tests/ -v -k "kubejob or job_state or reap"

# full suite before the PR
apps/backend/.venv/bin/pytest tests/ -m "not slow"
```

Expected: new tests pass; existing kubejob/job-state tests unchanged; cq-ratchet
reports "0 regressed" on both the ruff and the mypy half.

Note on the environment: the ratchet counts TID252 (relative imports),
untyped defs, PLC0415 (function-level imports) and DTZ006 (naive
`fromtimestamp`). Keep imports at module level and absolute, and type every new
helper and test helper.

## Rollback

`git revert <sha>` on the single squash-merged commit. The change is
control-plane logic only — no migration, no schema change, no gitops change, no
new env var — so a revert restores the previous behaviour exactly, and the only
consequence of reverting is that ref-less rows go back to being skipped.

No deployment step is required for the fix to be correct; it takes effect on the
next release that carries it.

## Out of scope

- Reproducing the crash live (killing the control plane mid-dispatch).
- Closing #1425 on the strength of these tests. The cap work needs its own
  measurement of per-build ephemeral disk use.
- `agent_kubejob.py:830-838` `_has_live_kubejob()`, a symptom that disappears
  once ref-less rows are reconciled.
