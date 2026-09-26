---
status: draft
issue: 1606
spec: spec/2026-09-26-1606-unreapable-slot-leak.md
---

# Plan: reconcile running rows whose worker was never claimed

> **Revision 2**, tracking spec revision 2. Revision 1 targeted a `running` row
> with `worker_ref = NULL`, a state that cannot occur. Steps 1–4 are rewritten;
> the reconstruction machinery and the collision guard carry over unchanged.

Self-contained summary of the approved decisions.

## Approved decisions (carried from the spec)

- **The premise.** `admit()` stamps every granted slot
  `worker_ref = {"kind": "subprocess"}` before any backend is chosen
  (`job_state_store.py:252`, `:265`, `:328`). NULL occurs only on the
  not-granted branch, so `running` + no ref is impossible. The kubejob backend
  overwrites the stamp later (`build_backend.py:1292`, documented at `:1226`).
- **The leak.** `running` + `{"kind": "subprocess"}` on a kubejob deployment.
  Nothing reaps it; no code outside `job_state_store` consumes a subprocess-kind
  row. It holds a concurrency slot permanently and silently.
- **The defect is the ambiguity**, not the missing reference: that state also
  describes a live subprocess build, and reaping one of those kills a real build.
- **The fix: stop asserting an unchosen backend.** The three admission sites
  stamp `{"kind": "pending"}`. `mark_running()` (`:284`) keeps writing
  `"subprocess"` — that is the subprocess path declaring itself, and it is what
  makes the states separable.

  | `kind` | meaning |
  | ------ | ------- |
  | `pending` | slot granted, unclaimed |
  | `subprocess` | subprocess path running it |
  | `k8s-job` | dispatched as a Job |

- **The reaper reconciles `pending` rows.** `get_active_kubejobs` returns them
  with `job_name`/`namespace` as `None`, keeps skipping `subprocess`.
  `reap_vanished_jobs` rebuilds the reference and ASKS the Kubernetes API — a
  `pending` row may still have a live Job, and assuming otherwise orphans it.
- **Reconstruction.** `job_dispatch.job_name(service, job_id)` is deterministic
  (`job_dispatch.py:185-192`); builds dispatch `job_id=task_id`
  (`build_backend.py:751,930`). Namespace as dispatch resolves it
  (`AIFACTORY_SANDBOX_NAMESPACE`, default `factory`).
- **Collision guard.** `_short()` keeps only the last 20 characters, so ids can
  collide. Before any terminal write, a resolved Job's `factory.io/job-id` label
  must equal `job_labels("aifactory", job_id)["factory.io/job-id"]` — the public
  function that stamped it, so the check cannot drift. Mismatch, missing label or
  API error → leave the row, log, fail closed.
- **Repair.** A verified reconstruction is persisted with `set_worker_ref`.
- **404 on a reconstructed name** is reaped with a distinct reason.
- **No reordering of the dispatch writes.** Reconciliation now covers a crash on
  either side of the ref write, so reordering buys nothing and touches the live
  dispatch path.
- **No migration.** `worker_ref` is nullable JSON (`models.py:1192`); a new
  `kind` value needs no schema change.

## Verified facts this plan relies on

- **Every reader of `worker_ref["kind"]` tests `!= "k8s-job"` and bails. None
  tests `== "subprocess"**, so the `pending` stamp changes no behaviour:
  `agent_kubejob.py:581` → `None`; `agent_kubejob.py:940` → `False`;
  `build_backend.py:1315` → `False`. Only `job_state_store.py:450` must change.
- `sanitize_log` is already imported at `build_backend.py:103`.
- The file's `core.*` imports are deliberately function-level (`core` joins
  `sys.path` at startup); a new one follows that convention, with the reason
  stated inline.
- `admit()` returns `"started"` / `"queued"` — a string, not a bool. Tests must
  assert on the string.
- The existing `_FakeBatch` (`tests/test_build_backend_kubejob.py`) returns a Job
  with `status` only and **no `metadata.labels`**, so it cannot exercise the
  label check. The new module carries its own fake that models labels rather than
  perturbing a 700-line file. (Deviation from revision 1, which named the wrong
  fake — a fake *store* in `test_kubejob_review_redrive.py:99`.)

## Steps

1. `tests/test_reap_refless_rows.py` — **pin the premise first**: assert a fresh
   `admit()` leaves the row `running` with `kind == "pending"`, and that
   `mark_running()` moves it to `"subprocess"`. Written before the production
   change so it fails first for the right reason. → verify by the test failing on
   `origin/dev` with `subprocess != pending`, then passing after step 2.

2. `apps/web-server/server/services/job_state_store.py:252`, `:265`, `:328` —
   stamp `{"kind": "pending"} if grant else None` at the three admission sites.
   Leave `mark_running()` (`:284`) writing `"subprocess"`. Update the docstrings
   that name the stamp. → verify by step 1's test passing.

3. `apps/web-server/server/database/models.py:1191` — document the third value
   in the `worker_ref` comment (`kind="pending"|"subprocess"|"k8s-job"`), noting
   that `pending` means granted-but-unclaimed. → verify by reading it back.

4. `apps/web-server/server/services/job_state_store.py:450` — in
   `get_active_kubejobs`, return `pending` rows alongside `k8s-job` rows, with
   `job_name`/`namespace` as `None`; keep skipping `subprocess` and any other
   kind. Docstring explains why a `pending` row is this reaper's business.
   → verify by unit tests for all three kinds.

5. `apps/web-server/server/services/build_backend.py` — module-level helpers:
   `_dispatch_namespace()`, `_reconstructed_ref(job_id)` (lazy
   `core.job_dispatch.job_name`, reason inline), `_job_id_label(job)`.
   → verify by the reconstruction tests.

6. `apps/web-server/server/services/build_backend.py` — methods
   `_reconstructed_job_is_ours(batch, namespace, job_name, job_id)` (label check
   via the public `job_labels`, fails closed on any error) and
   `_repair_worker_ref(job_id, job_name, namespace)` (best-effort `set_worker_ref`,
   logs the repair). → verify by the collision and repair tests.

7. `apps/web-server/server/services/build_backend.py:1394-1400` — in
   `reap_vanished_jobs`, replace the `if not job_name or not namespace: continue`
   bail (and its comment, which points at unreachable code) with: reconstruct
   when either is missing, call the unchanged `_job_outcome`, then for a
   reconstructed row that is not `gone` require `_reconstructed_job_is_ours`
   before proceeding and repair the ref. Give the `gone` branch a reason that
   distinguishes the reconstructed case. → verify by the four-outcome tests.

8. `tests/test_reap_refless_rows.py` — the remaining cases from the spec's
   verification list: query behaviour per kind, the live-Job regression, all four
   outcomes, the collision guard (asserting on the log), the unlabelled-Job
   fail-closed case, slot return via `admit()` at the same cap, and ref
   persistence. Rename the module if `refless` stops describing it. → verify by
   the commands below.

9. Commit code and tests together, then run both halves of cq-ratchet (it diffs
   committed history, so after the commit). → verify by "0 regressed" from each.

10. Open the PR against `dev` linking intent, spec (rev 2) and plan (rev 2), and
    noting the revision-1 falsification so review sees why the design changed.
    → verify by the PR body containing all three paths and `Closes #1606`.

## Tests

```bash
# the new module
apps/backend/.venv/bin/pytest tests/test_reap_refless_rows.py -v

# the regression net for the `pending` stamp — these must pass UNCHANGED
apps/backend/.venv/bin/pytest tests/ -v -k "kubejob or job_state or reap"

# full suite before the PR
apps/backend/.venv/bin/pytest tests/ -m "not slow"
```

Expected: new tests pass; the kubejob/job-state/reaper suites pass unchanged;
cq-ratchet "0 regressed" on both halves.

Ratchet notes: it counts TID252 (relative imports), untyped defs, PLC0415
(function-level imports) and DTZ006 (naive `fromtimestamp`). Type every new
helper **and** every test helper. The one new function-level `core.*` import
follows the file's existing, documented convention; if the ratchet counts it as a
regression, add a `noqa` naming the sys.path reason rather than hoisting it.

## Known limitation

Rows written before this change carry `subprocess` and stay invisible to the
reaper. Accepted deliberately: the live table holds 0 `running` and 0 `queued`
rows, so a backfill would add risk without removing any. Stated here so the next
reader does not mistake it for an oversight.

## Rollback

`git revert <sha>` on the single squash-merged commit. Control-plane logic only —
no migration, no schema change, no gitops change, no new env var — so a revert
restores the previous behaviour exactly. A row stamped `pending` by the reverted
code would read as "not a k8s-job" to every consumer, which is how a
`subprocess`-stamped row already reads; no row is left unreadable.

No deployment step is needed for the fix to be correct; it takes effect on the
next release carrying it.

## Out of scope

- Reproducing the crash live (killing the control plane mid-dispatch).
- Closing #1425 on the strength of these tests; the cap needs its own
  measurement of per-build ephemeral disk use.
- `agent_kubejob.py:830-838` `_has_live_kubejob()`, a symptom that disappears
  once these rows are reconciled.
- Backfilling historical `subprocess`-stamped rows (see Known limitation).
