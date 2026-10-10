---
status: approved
issue: 1677
spec: spec/2026-10-10-1677-kubejob-pre-worker-ref-job.md
---

# Plan: a kubejob build whose worker ref write fails keeps running unseen

Branch `fix/1677-kubejob-pre-worker-ref-job`, base `origin/dev` (1c2df772).
Line numbers checked at HEAD 43e87358. The spec's numbers for `set_worker_ref`,
`delete_job` and its ref read are one line low; this plan uses the HEAD numbers.

## Approved decisions (self-contained summary)

1. **Strategy (Q1).** When `set_worker_ref` fails, `KubeJobBuildBackend.dispatch`
   deletes the Job it just created and re-raises. The rule stays: a raise from
   `dispatch` means nothing is running. The ref is NOT written before the Job is
   created. A `k8s-job` ref forces the row to running (#1628,
   `job_state_store.py:419-421`), and the #1606 reaper would then read the
   not-yet-created Job as gone.
2. **Callers unchanged.** `agent_kubejob.py:362-366` (`except Exception`,
   releases the credential, re-raises), `agent_service.py:921-924` (marks the
   row failed, "spawn failed during admission") and `agent_queue.py:156-170`
   (marks failed, "spawn failed on dequeue") all still see the original error.
3. **Rollback delete also fails (Q2).** Log at ERROR with the traceback
   (`_log.exception`), passing namespace, job_name and task_id through
   `sanitize_log`, then re-raise the ORIGINAL `set_worker_ref` error, never the
   delete error. No new reaping path. The orphan is bounded by
   `activeDeadlineSeconds` (`_DEFAULT_DEADLINE_SECONDS` = 6h,
   `build_backend.py:185`, applied at `:675`). The approver accepted this
   double-failure gap.
4. **#1667 stamp (Q3).** `stamp_spawn(..., "kubejob")` at `build_backend.py:1285`
   stays as is: a Job really was created. Do not touch `trusted_contract_store`.
5. **Scope (Q4).** Fix only the untracked Job. The double-failure residue (a
   label sweep by `factory.io/job-id` for Jobs with no running row, using the
   existing list RBAC; holding the credential until the Job is gone) goes into
   ONE new follow-up issue, not #1669 or #1670.
6. **Shape of the fix.** Edit only `dispatch`. Move `set_worker_ref` inside the
   existing `try`, right after `create_namespaced_job`, so the owned client is
   still open for cleanup and the existing `finally` closes it exactly once on
   every path. Wrap only the ref write in its own `except BaseException:`
   (a `CancelledError` during the write is the same bug; the bare `raise`
   hands the cancel back). Inside, call
   `await batch.delete_namespaced_job(job_name, namespace, propagation_policy="Background")`
   (the shape of `delete_job` at `:1329-1331`), catch its failure with
   `except Exception` + `_log.exception`, then bare `raise`. The inner
   `except Exception` carries `# noqa: BLE001` with a reason (strict ruff
   flags it even with `_log.exception`; the outer one re-raises, so it does not).
7. **Target.** Act only on the manifest's `job_name`/`namespace`
   (`:1276-1277`). No rebuilt name, no label match, no `delete_collection`
   (the `deletecollection` verb is not granted). Do not reuse `delete_job()`
   (`:1307-1343`): it reads the ref from the row (`:1314-1323`), which was never
   written, so it returns False.
8. **Not handled, on purpose.** No rollback when `create_namespaced_job` itself
   raises (409 = another dispatch owns the name; a timeout after server
   acceptance belongs to the follow-up sweep). No 404 special case on the
   rollback delete (the ERROR log on that race is accepted). No
   `asyncio.shield`, no retry.
9. **Success path.** Same calls, same order; only the client close moves to
   after the ref write. The `_log.info` line and `return job_name` are unchanged.
10. **Cancel-path freeing (verified).** Callers catch only `Exception`, so on a
    `CancelledError` the row stays running with its admit-time ref
    `{kind: pending}` (`job_state_store.py:44`, `:262`/`:275`).
    `get_active_kubejobs` includes pending rows (`job_state_store.py:445`,
    filter `:483`). `reap_vanished_jobs` (`build_backend.py:1478`, via
    `_resolve_row_ref` `:1438`) rebuilds the Job name, gets 404 because the Job
    was deleted, and `_fail()`s the row on the "gone" path (`:1550-1557`),
    driven from `agent_kubejob.py:786/812`. The pooled credential on the cancel
    path is still not released by the caller (existing behaviour, unchanged;
    noted in the follow-up issue).
11. **No rebase.** The branch contains #1691 (1c2df772). Its `child_env` lines at
    `build_backend.py:109` and `:953` stay untouched.
12. **Model split.** 2 files, 2 code steps: below the coder handoff threshold.
    The session model implements all steps.

Shared setup for every command:

```sh
export PATH=/mnt/code/Source-home/GitHub/AIFactory/apps/backend/.venv/bin:$PATH
cd /mnt/code/Source-home/GitHub/AIFactory-1677
```

## Steps

1. `tests/test_build_backend_kubejob.py:15-24, 518-525, insert before 528`:
   red tests.
   - Imports: add `import asyncio` and `import logging` as plain one-line
     stdlib imports, sorted `asyncio, json, logging, sys`. No aliases, no new
     `from` lines.
   - `test_dispatch_records_worker_ref` (495-525): add `assert fake.deleted == []`
     after the worker_ref dict assertion (T0).
   - Before `test_dispatch_injects_oauth_token_into_job_env` (528), add three
     async tests with the setup of 495-525: `setenv AIFACTORY_DATA_ROOT`, stub
     `bb.populate_build_worktree`, `_make_store`, `admit("p:s1", _spawn_args("s1"), cap=2, correlation_key="9")`,
     then `monkeypatch.setattr(store, "set_worker_ref", _boom)` with
     `async def _boom(*_a: Any, **_k: Any) -> None: raise err`.
     - T1 `test_dispatch_deletes_job_when_worker_ref_write_fails`:
       `err = RuntimeError("db down")`. Batch is a local
       `class _KwBatch(_FakeBatch)` whose `delete_namespaced_job` stores
       `self.kw = _kw` then calls `super()`. Assert
       `pytest.raises(RuntimeError) as exc` + `exc.value is err`;
       `name = fake.created[0][1]["metadata"]["name"]`;
       `fake.deleted == [("factory", name)]`;
       `fake.kw == {"propagation_policy": "Background"}`;
       `(await store.get_state("p:s1"))["worker_ref"].get("kind") != "k8s-job"`
       (documentation only; the stub can never write the ref).
     - T2 `test_dispatch_reraises_original_error_when_rollback_delete_fails`:
       local `class _FailDelete(_FakeBatch)` whose
       `async def delete_namespaced_job(self, *_a, **_kw)` raises `_ApiError(500)`
       (`_ApiError(status: int)`, :48-51). Under
       `caplog.at_level(logging.ERROR, logger=bb.__name__)` and
       `pytest.raises(RuntimeError) as exc`: assert `exc.value is err`;
       `recs = [r for r in caplog.records if r.levelno == logging.ERROR and name in r.getMessage()]`;
       `assert recs`; `assert isinstance(recs[0].exc_info[1], _ApiError)`.
     - T3 `test_dispatch_deletes_job_when_cancelled_during_worker_ref_write`:
       `_boom` raises `asyncio.CancelledError()`;
       `pytest.raises(asyncio.CancelledError)`;
       `fake.deleted == [("factory", name)]`.
   → verify by `python -m pytest tests/test_build_backend_kubejob.py -q`:
   T1, T2, T3 fail, the other 74 pass (T0 passes on old code). Then
   `ruff format --check tests/test_build_backend_kubejob.py && ruff check tests/test_build_backend_kubejob.py`.
   Traps: do not change `_ApiError` or `_FakeBatch` (:48-90);
   `tests/test_trusted_contract_isolation_stamp.py:33` imports `_FakeBatch`.
   Subclass inside the tests only. The CancelledError must come from the
   monkeypatched coroutine, not from cancelling a task (pytest-asyncio 1.3.0
   needs no `uncancel()`). `bb._log` is `logging.getLogger(__name__)` (:117)
   with no `propagate=False`, so caplog sees it. `tests/pytest.ini` already sets
   `asyncio_mode = auto`. This step is red: do not commit it alone; it lands in
   the step 2 commit.

2. `apps/web-server/server/services/build_backend.py:1287-1298` (`dispatch`
   only): keep `try:` (1287) and `await batch.create_namespaced_job(namespace, manifest)`
   (1288). Directly after it, still inside the outer try, add:

   ```python
           try:
               await self._store.set_worker_ref(
                   task_id,
                   {"kind": "k8s-job", "namespace": namespace, "job_name": job_name},
               )
           except BaseException:
               # #1677: a raise from dispatch must mean nothing is running.
               try:
                   await batch.delete_namespaced_job(
                       job_name, namespace, propagation_policy="Background"
                   )
               except Exception:  # noqa: BLE001 - logged; the original error wins
                   _log.exception(
                       "[build_backend] could not roll back k8s Job %s/%s for task %s "
                       "after the worker_ref write failed",
                       sanitize_log(namespace),
                       sanitize_log(job_name),
                       sanitize_log(task_id),
                   )
               raise
   ```

   Keep the `finally:` owned-client close (1289-1293) unchanged. Delete the old
   post-finally `set_worker_ref` call (1295-1298). Lines 1299-1305 (`_log.info`,
   `return job_name`) stay. Optional: add one sentence to the docstring
   (1221-1243): "If the worker_ref write fails, the just-created Job is deleted
   and the original error re-raised (#1677), so a raise still means nothing is
   running."
   → verify by `python -m pytest tests/test_build_backend_kubejob.py -q`
   (77 passed), then every command in Tests.
   Commit (with step 1): `fix(kubejob): delete the Job when its worker_ref write fails (#1677)`,
   body names plan steps 1 and 2.
   Traps: outer handler is `except BaseException` (do not narrow it; T3 is the
   only guard), inner is `except Exception`. Strict ruff (standards/ruff.toml)
   flags the inner one as BLE001 despite `_log.exception` (checked: without the
   noqa the ratchet reports `BLE001 +1`), so keep the `# noqa: BLE001 - reason`
   on that `except` line; the outer `except BaseException` re-raises and is not
   flagged, so give it no noqa (RUF100 would flag an unused one). `raise` must
   be bare. Strict line-length is 100 and `ruff format` does not split strings:
   a one-string log message is 118 columns and the ratchet reports `E501 +1`.
   Keep it as two implicitly concatenated literals, as above. Do not touch
   `child_env` lines (:109, :953), `stamp_spawn` (:1285) or `delete_job`
   (:1307-1343). No `asyncio.shield`, retry, 404 case or `delete_collection`.
   `batch` stays inside the same try, so whatever typing the existing
   `create_namespaced_job` call gets, the delete gets too. Scope may not
   contain `#`.

3. `CHANGELOG.md:1` (`## [Unreleased]`, Fixed): "Kubernetes builds: if
   recording a dispatched Job fails, the Job is deleted instead of running
   untracked (#1677)." Then open ONE follow-up issue (label sweep listing Jobs
   by `factory.io/job-id` with no running row using the existing list RBAC;
   hold the pooled credential until the Job is confirmed gone; the
   double-failure orphan bounded only by the 6h deadline, `build_backend.py:185/:675`;
   the cancel path not releasing the pooled credential). Do not fold it into
   #1669 or #1670. Then the PR against `dev`.
   → verify by `git diff --stat origin/dev -- . ':!intent' ':!spec' ':!plan'`
   listing exactly the 2 code files plus `CHANGELOG.md`.
   Commit: `docs(changelog): note kubejob worker_ref rollback (#1677)`.
   Traps: commit on the task branch, never on main; base is `dev`. The PR
   links intent, spec and plan; says the session model did all steps (below
   the coder threshold); includes the cancel-path argument (decision 10); names
   rebase overlaps: #1669/#1670 (`agent_kubejob.py`, not edited here, recheck
   that 362-366 still re-raises), #1671 (`child_env`/`RUNNER_KEEP` near
   `build_backend.py:109/:953`, expect a textual conflict; if it lands first,
   rebase and re-check those lines), #1673 (`tests/test_child_process_env.py`)
   and #1672 (`pr_endgame.py`) do not touch these files. End with the
   attribution footer.

## Tests

1. `python -m pytest tests/test_build_backend_kubejob.py -q` → 77 passed
   (baseline 74).
2. `python -m pytest -q tests/test_agent_kubejob_mixin.py tests/test_agent_service_kubejob_backend.py tests/test_is_running_kubejob.py tests/test_reap_unclaimed_slots.py tests/test_exit_does_not_bury_a_kubejob.py tests/test_kubejob_liveness.py tests/test_failed_build_reaches_the_task.py tests/test_trusted_contract_isolation_stamp.py`
   → 56 passed, unchanged (2/9/10/11/3/7/10/4).
   `test_12_kubejob_dispatch_stamps_before_job_is_created` must still pass.
3. `python -m pytest -q -o asyncio_mode=auto apps/web-server/tests/test_task_branch.py apps/web-server/tests/test_control_plane_reads_the_pushed_work.py`
   → 15 passed, unchanged.
4. `python -m pytest tests/test_no_unscrubbed_spawn.py -q` → passes.
5. `python -m pytest tests -q` → baseline count + 3.
6. `ruff format --check apps/backend apps/web-server scripts tests && ruff check apps/backend apps/web-server scripts tests` → clean.
7. `git add -A && python scripts/cq_ratchet.py --staged --ruff "$(command -v ruff)" --config standards/ruff.toml --paths 'apps/backend/*.py' 'apps/web-server/*.py' 'scripts/*.py'`,
   then the same with `--tool mypy --mypy "$(command -v mypy)" --config standards/mypy.ini`
   → no new findings. The ruff ratchet counts per rule code: `build_backend.py`
   has 10 pre-existing strict findings, and any code whose count rises fails
   (checked against the step 2 snippet: 0 regressed; the one-string message and
   a bare inner `except Exception` gave `E501 +1`, `BLE001 +1`). The test file
   is outside `--paths`; default `ruff check` covers it.
8. `python scripts/gen_autonomy_matrix.py --check` → passes (nothing cited
   moved; no matrix file cites these files).
9. `git diff --stat origin/dev -- . ':!intent' ':!spec' ':!plan' ':!CHANGELOG.md'`
   → exactly `build_backend.py` and `tests/test_build_backend_kubejob.py`.
   `git diff origin/dev -U0 -- apps/web-server/server/services/build_backend.py | grep -nE '^@@'`
   → no hunk touches lines 109 or 953.
10. Mutation checks (scratch edits to `dispatch`, revert each with
    `git checkout -- apps/web-server/server/services/build_backend.py`, never
    commit one). Each must fail the listed tests:

    | # | Mutation | Fails |
    |---|---|---|
    | M0 | old order, no rollback | T1, T2, T3 |
    | M1 | `except BaseException` → `except Exception` | T3 |
    | M2 | drop inner try/except around the delete | T2 |
    | M3 | `_log.exception(...)` → `pass` | T2 |
    | M4 | drop the bare `raise` | T1, T2, T3 |
    | M5 | swap `job_name, namespace` args | T1, T3 |
    | M6 | drop `propagation_policy` | T1 (`fake.kw`) |
    | M7 | delete unconditionally (e.g. in outer `finally`) | T0 (and T1, T3: two deletes) |

Skipped: no end-to-end reaper test for the cancel path. `_resolve_row_ref`
and `reap_vanished_jobs` are unchanged and covered by
`test_reap_unclaimed_slots.py`.

## Rollback

Only 2 code files plus CHANGELOG; no schema, chart or RBAC change, no data
migration.

- Before merge: `git restore --source=origin/dev -- apps/web-server/server/services/build_backend.py tests/test_build_backend_kubejob.py CHANGELOG.md`.
- After merge: `git revert <merge-sha>` on dev, then re-run Tests 1 and 2
  (count back to 74).
- What a revert gives up: a failed ref write again leaves an untracked Job
  running until its 6h deadline (`build_backend.py:185/:675`).

## Deviations

- Step 1: T1-T3 share a helper `_dispatch_with_failing_ref(tmp_path,
  monkeypatch, err, fake)`; assertions unchanged. T2 also asserts
  `exc_info is not None`. T1's documentation-only ref-kind assert is left
  out (the helper does not expose the store; the plan says it cannot fail).
- Step 3: M7 as applied (delete moved into the outer `finally`, rollback
  delete removed) is caught by T0 and T2, not T1 and T3; the plan's two-delete
  variant was not applied.
