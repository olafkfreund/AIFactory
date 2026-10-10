---
status: draft
issue: 1677
intent: intent/2026-10-10-1677-kubejob-pre-worker-ref-job.md
---

# Spec: a kubejob build whose worker ref write fails keeps running unseen

## Design

Checked against branch `fix/1677-kubejob-pre-worker-ref-job` at d82e4bc7. The
branch already contains #1691 (1c2df772 is an ancestor; `child_env` is at
`build_backend.py:109` and `:953`), so no rebase is needed before the change.

### Proposed answers to the intent's open questions

These are proposed defaults for the approver to confirm or change at this
gate. They are not approved yet.

1. **Strategy: delete the Job when the ref write fails.** Keep the rule that a
   raise from `dispatch` means nothing is running, so the three callers stay
   unchanged (`agent_kubejob.py:362-366`, `agent_service.py:921-924`,
   `agent_queue.py:160-170`). Writing the ref before the Job exists is
   rejected: a `k8s-job` ref forces the row to running (#1628,
   `job_state_store.py:419-421`), and the #1606 reaper would then read the
   not-yet-created Job as gone. That is a contract change for all three callers.
2. **If the cleanup delete also fails:** log it at error level with
   `sanitize_log` on namespace, job name and task id, and re-raise the
   original `set_worker_ref` error. No new reaping path in this PR. The orphan
   is bounded by `activeDeadlineSeconds` (6h by default, `build_backend.py:185`,
   `:675`). The callers mark the row failed and release the credential, as
   today. File a follow-up for a guaranteed sweep (list Jobs by the
   `factory.io/job-id` label that have no running row; the chart's existing
   list RBAC covers it).
3. **The #1667 spawn stamp:** leave it. `stamp_spawn(..., "kubejob")` at
   `:1285` records how the build is isolated, and a Job really was created, so
   it stays true. Correcting it would touch `trusted_contract_store`, which
   #1683, #1686 and #1687 own and the intent forbids.
4. **Scope:** fix only the untracked Job. When the compensating delete
   succeeds, the early credential release, the freed slot and the missing log
   stream are all correct, because the Job is gone. They remain only when the
   delete also fails (Q2), which goes into one follow-up issue (label sweep
   plus holding the credential until the Job is gone), not into #1669 or #1670.

### Change: `apps/web-server/server/services/build_backend.py`, `dispatch` only

Today (`:1287-1298`) the Job is created inside a `try` whose `finally` closes
the client this call created, and `set_worker_ref` runs after that `finally`
with no error handling. `delete_job()` (`:1306-1342`) cannot be reused for
cleanup: it reads the ref from the row (`:1316-1322`), which was never
written, so it returns False and deletes nothing. Its call shape is copied
instead.

Replace `:1287-1298` with:

```python
        try:
            await batch.create_namespaced_job(namespace, manifest)
            try:
                await self._store.set_worker_ref(
                    task_id,
                    {"kind": "k8s-job", "namespace": namespace, "job_name": job_name},
                )
            except BaseException:
                # #1677: callers read a raise as "nothing running". Delete the Job
                # this call created, by its manifest name. Not delete_job(): the
                # row has no ref. BaseException so a cancel mid-write also cleans up.
                try:
                    await batch.delete_namespaced_job(
                        job_name, namespace, propagation_policy="Background"
                    )
                except Exception:
                    # ponytail: orphan bounded by activeDeadlineSeconds (6h,
                    # :185/:675); a label sweep is the follow-up.
                    _log.exception(
                        "[build_backend] rollback delete of Job %s/%s for task %s failed",
                        sanitize_log(namespace), sanitize_log(job_name), sanitize_log(task_id),
                    )
                raise  # the original set_worker_ref error, not the delete error
        finally:
            if owns_client:
                ...  # unchanged close
```

- `job_name` and `namespace` come from the manifest built earlier in
  `dispatch`, so the delete acts only on the Job this dispatch created.
- The bare `raise` re-raises the `set_worker_ref` error (or the cancel),
  because the inner handler has finished. Callers keep recording "spawn failed
  during admission" / "spawn failed on dequeue".
- The cleanup uses the already-open client; the `finally` now runs after it,
  so an owned client is still closed exactly once on every path.
- `except BaseException` (taken from the robust design): a `CancelledError`
  arriving while `set_worker_ref` awaits is the same bug by another route. A
  cancel is delivered once, so the delete's `await` still runs, and the bare
  `raise` hands the cancel back to the caller.
- The `_log.info` success line and `return job_name` are unchanged. On the
  success path the same calls run in the same order; only the client close
  moves after the ref write.

### Tests: `tests/test_build_backend_kubejob.py`

Follow `test_dispatch_records_worker_ref` (`:495`), using `_make_store`,
`admit`, the `populate_build_worktree` stub and `_FakeBatch` (whose
`delete_namespaced_job`, `:84-88`, records `(namespace, name)` and accepts
`**_kw`). Monkeypatch `store.set_worker_ref` with an async function that raises.
No fake changes are needed.

1. `test_dispatch_deletes_job_when_worker_ref_write_fails`: write raises
   `RuntimeError("db down")`; the same exception propagates;
   `fake.deleted == [("factory", fake.created[0][1]["metadata"]["name"])]`; the
   row has no `k8s-job` worker_ref.
2. `test_dispatch_reraises_original_error_when_rollback_delete_fails`: a
   `_FakeBatch` subclass whose delete raises `_ApiError(500)`; the
   `RuntimeError` propagates, not the `_ApiError`; `caplog` has an ERROR record
   naming the job.
3. `test_dispatch_deletes_job_when_cancelled_during_worker_ref_write`: write
   raises `asyncio.CancelledError`; the delete is recorded and `CancelledError`
   propagates.
4. Existing `test_dispatch_records_worker_ref`: add `assert fake.deleted == []`.

## Alternatives rejected

- **Write the ref before creating the Job:** forces the row to running (#1628)
  and makes the #1606 reaper read a missing Job as gone; a contract change for
  all three callers.
- **Reuse `delete_job()`:** reads the ref from the row, which is absent here,
  so it returns False.
- **Clean up in the three callers:** three edits instead of one, and the
  callers do not have the manifest name.
- **Delete by the `factory.io/job-id` label with `delete_collection`:** needs
  the deletecollection verb the chart does not grant, and could hit Jobs from
  an earlier attempt of the same task.
- **Also roll back when `create_namespaced_job` raises:** a 409 means the name
  belongs to another dispatch; a timeout after server acceptance predates this
  issue and belongs to the follow-up sweep.
- **Treat a 404 on the rollback delete as success (no error log):** the
  outcome is already right either way; a stray error line on a race is
  acceptable and saves a branch.
- **`asyncio.shield` or retrying the delete:** the `finally` closes the client
  right after, and retries add latency to the request path for a two-failure
  case. The follow-up sweep is the guaranteed path.
- **A label-based orphan reaper now / correcting the #1667 stamp:** see Q2-Q4.

## Risks

- **Store and delete both fail:** the Job runs orphaned up to
  `activeDeadlineSeconds` (6h) holding a credential the pool has released.
  Same as today, now logged at error level. Covered by the follow-up.
- **Background delete is asynchronous:** the pod may run for a few seconds
  after `dispatch` raises and may already have touched the worktree. The row
  ends up failed and recovery resets the worktree as after any failed build.
- **A second cancel during cleanup** interrupts the delete and leaves the
  orphan case above. Rare; accepted.
- **Ref write committed but raised anyway** (connection dropped after commit):
  the row holds a `k8s-job` ref, the Job is deleted, the caller marks the row
  failed; if that also fails, the #1606 reaper sees a 404 and takes its "gone"
  path. Each combination ends consistent.
- **Scope:** kubejob installs only. Subprocess backends never reach
  `KubeJobBuildBackend.dispatch`. Only the jobs `delete` verb is used, which
  the chart already grants.

## Verification

- `pytest tests/test_build_backend_kubejob.py -q`: the 3 new tests pass and
  the existing ones still pass.
- The kubejob reap, `is_running` and mixin suites named in the intent pass
  unchanged.
- `ruff check apps/web-server/server/services/build_backend.py tests/test_build_backend_kubejob.py`
- `git diff --stat origin/dev` (code commits) touches exactly those 2 files:
  nothing in the callers, `job_state_store`, `routes/execution.py`, the RBAC
  chart or `trusted_contract_store`. The #1691 lines `:109` and `:953` are
  untouched.
