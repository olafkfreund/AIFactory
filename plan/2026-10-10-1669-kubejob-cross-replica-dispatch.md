---
status: approved
issue: 1669
spec: spec/2026-10-10-1669-kubejob-cross-replica-dispatch.md
---

# Plan: every replica sees a kubejob build as running, not only the one that dispatched it

Worktree: `/mnt/code/Source-home/GitHub/AIFactory-1669`, branch
`fix/1669-kubejob-cross-replica-dispatch`. Paths are relative to the repo
root. Every verify command runs from the worktree root after:

```
export PATH=/mnt/code/Source-home/GitHub/AIFactory/apps/backend/.venv/bin:$PATH
```

The new and touched tests are root tests (`python -m pytest tests/<file> -q`),
not the web-server suite.

## Approved decisions (self-contained summary)

1. **Problem.** With the kubejob build backend, only the replica that
   dispatched a build has the task in memory, so the sync
   `AgentService.is_running` answers False on every other replica. With
   `replicaCount > 1` (`charts/aifactory/values.yaml:32`) or the HPA
   (`:191-194`) on and rmux off, another replica shows the card idle, lets
   Recover double-start the build, and 404s Stop. Fix now.
2. **Shape.** One new async method `is_running_anywhere(task_id)` in
   `apps/web-server/server/services/agent_kubejob.py`, placed immediately
   before `_kubejob_liveness`. The sync `is_running`
   (`services/agent_service.py:1560-1569`) is unchanged. Order follows the
   reaper: in-memory first, then the store.
3. **Predicate body, exactly:**
   ```python
   _LIVE_KINDS = ("k8s-job", "pending")  # subprocess rows: pod-local, see Q6

   async def is_running_anywhere(self, task_id: str) -> bool:
       if self.is_running(task_id):
           return True
       if not getattr(self, "_store_enabled", False) or not self._kubejob_backend_enabled():
           return False
       try:
           state = await self._store().get_state(task_id)
       except Exception:  # noqa: BLE001
           _log.warning(
               "[AgentService] job-state read failed for %s; reading as running (#1669)",
               sanitize_log(task_id),
           )
           return True
       return (
           state is not None
           and state.get("lifecycle_state") == "running"
           and (state.get("worker_ref") or {}).get("kind") in _LIVE_KINDS
       )
   ```
   Add a short docstring naming #1669. `_TERMINAL_STATES` is not used.
4. **Cost (Q3).** One primary-key read per call, only when the in-memory
   answer is False and the kubejob backend is on. `_kubejob_backend_enabled`
   (`agent_kubejob.py:104-124`) already requires the store. The default
   subprocess backend never reads the store here, even with `DATABASE_URL`
   set. `_store_enabled` is checked first so a kubejob-without-store
   misconfiguration does not log `_kubejob_backend_enabled`'s warning on every
   status poll.
5. **Failure (Q4).** Fail closed: a store exception reads as running. Recover
   returns 409, Stop proceeds to `stop_task`, status shows running. No
   `exc_info` on the warning: a DB outage would flood the log with tracebacks
   on every status poll. A missing row, a terminal row (`done`, `failed`,
   `stuck`, `review`), a `queued` row, and a `running` row whose
   `worker_ref.kind` is `subprocess` all read as not running.
6. **Kinds (Q6).** Only `k8s-job` and `pending` (granted slot not yet claimed,
   #1606) count. An in-pod (`subprocess`) build's row is never marked terminal
   if its pod dies, and on the subprocess backend rows keep `pending` kind for
   life; the backend gate plus the kind filter stop a restarted single replica
   from showing a card running forever with Recover stuck at 409. Orphaned
   `pending` rows on kubejob are already reaped.
7. **Scope (Q1, Q9).** Four readers switch to the new predicate:
   `GET /{id}/status`, `GET /{id}/running` (the badge, read by
   `apps/frontend-web/src/lib/api-adapter.ts:397`), Stop, and Recover's
   refusal. `_refuse_recovery_while_running` becomes `async` and is awaited
   in `recover_task`. Start (`routes/execution.py:690`), plan approval
   (`routes/plan_approval.py:170`), `GET /running` list (`get_running_tasks`,
   `execution.py:246-249`), `websockets/progress.py`, the reaper,
   `_kubejob_liveness` and `_stop_kubejob_build` do not change. Moving Start
   or plan approval would make every replica delete live Jobs; a follow-up
   issue decides.
8. **Stop.** After the 404 guard, `stop_task` already works across replicas
   through `_stop_kubejob_build` (reads the store, deletes the Job). No change
   there.
9. **No chart change (Q10).** The replica pin stays.
10. **Sequencing (Q7).** Separate PRs: #1669, then #1670 (log streamer reuses
    this predicate), then #1677.
11. **Alternatives rejected** (do not implement): making `is_running` async or
    store-backed; reusing `_kubejob_liveness` (missing row means unknown, so
    never-built tasks would show running); `JobStateStore.is_running` (counts a
    dead pod's in-pod row, lets exceptions escape); counting every
    non-terminal row including `queued`; inlining the read in each route;
    an `asyncio.wait_for` timeout; a Stop-specific 409; caching the answer.
12. **Risks accepted.** Store outage gives Recover 409 and Stop 500 for its
    duration. No read timeout. A missed `await` on
    `_refuse_recovery_while_running` silently never raises (only caller
    `execution.py:1074`; tests catch it). Stop on another replica's `pending`
    row returns 500 until `set_worker_ref` stamps `k8s-job` (today 404); that
    window belongs to #1677 and gets no test here.

## Deviations from the spec

- **D1.** The spec allows "two fake updates" in tests. A third test edit is
  needed: `tests/test_is_running_kubejob.py:250` and `:253` call
  `execution._refuse_recovery_while_running(...)` synchronously inside an
  async test. Once the helper is `async def`, `:250` returns an unawaited
  coroutine and its `pytest.raises(HTTPException)` fails. Both calls get
  `await`.
- **D2.** `tests/audit/test_task_action_audit.py` does not import `AsyncMock`
  (imports at `:9-27`). Instead of the spec's `AsyncMock`, follow the
  fixture's existing local `async def _stop` pattern (`:90-91`).
- **D3 (line drift only, no code affected).** `set_worker_ref` calls are at
  `services/build_backend.py:1295` and `:1420`; `_TERMINAL_STATES` is defined
  at `:356`.

## Repo traps (every step)

- `ruff check` and `ruff format --check` with the default config on every
  touched file.
- One single-name import per line, any `noqa` on that single line, no aliased
  multi-name imports, so CI ruff and `scripts/cq_ratchet.py --staged` agree.
- No test may spawn a real subprocess (`test_no_unscrubbed_spawn`); Stop uses a
  faked `delete_job`.
- Commit scope must not contain `#`. End every commit with the session's
  attribution trailers.

## Steps

1. `tests/test_is_running_anywhere.py` (new), `tests/test_recover_refuses_live_build.py:35-41`,
   `tests/audit/test_task_action_audit.py:88-99`: red tests and fakes →
   verify by `python -m pytest tests/test_is_running_anywhere.py -q`: every
   predicate test fails with `AttributeError: ... is_running_anywhere`; the
   route tests fail on their assertions (status says False, Stop 404s, Recover
   does not raise 409), except `test_route_stop_404_without_row` and
   `test_route_recover_force_proceeds`, which already pass; and
   `python -m pytest tests/test_recover_refuses_live_build.py tests/audit/test_task_action_audit.py -q`
   still passing.
   - `_Agent` fake: add
     `async def is_running_anywhere(self, _t): return self._running`.
   - Audit `stoppable` fixture (D2): add
     ```python
     async def _running_anywhere(_t):
         return True
     ```
     and pass `is_running_anywhere=_running_anywhere` in the `SimpleNamespace`
     at `:96`, keeping `is_running`.
   - New file setup: put `apps/web-server` and `apps/backend` on `sys.path`
     as `tests/test_is_running_kubejob.py:23-30` does; import `AgentService`,
     `JobStateStore`, `SpawnArgs`, `Base`, `execution` one per line, each
     `# noqa: E402`. Copy `_make_factory` and the autouse `_dispose_engines`
     from `tests/test_job_state_store.py:43-61`.
   - Fixture `pair(tmp_path, monkeypatch)`: `setenv("AIFACTORY_BUILD_BACKEND",
     "kubejob")`; one SQLite file, engine 1 runs `create_all`, engine 2 opens
     the same file (pattern `tests/test_job_state_store.py:255-265`).
     `a, b = AgentService(), AgentService()`, each `_store_enabled = True`,
     `a._job_store = JobStateStore(session_factory=f1)`,
     `b._job_store = JobStateStore(session_factory=f2)` (`_store()` returns
     `_job_store` when set, `agent_service.py:203`).
   - Helper `row(state, kind)` writes through `a._job_store`:
     `running/pending` = `admit(TASK, spawn, cap=0)`; `running/k8s-job` =
     admit then `set_worker_ref(TASK, {"kind": "k8s-job", "job_name": "j",
     "namespace": "factory"})`; `running/subprocess` = admit then
     `mark_running`; `queued` = admit another task with `cap=1`, then TASK
     with `cap=1`; terminal = k8s-job row then `mark_terminal(TASK, s,
     error="x")` (keeps `kind: k8s-job`, so it also catches a dropped
     lifecycle check).
   - `_CountingStore`: `get_state` appends to `calls` and raises
     `RuntimeError`.
   - Predicate tests (each also asserts `b.is_running(TASK) is False` unless
     noted): see the table in Tests.
   - Route tests on B: `monkeypatch.setattr(execution, "get_agent_service",
     lambda: b)`, `emit_task_status` and `audit_task_action` patched to async
     no-ops; Recover reuses the `project` fixture approach from
     `tests/test_recover_refuses_live_build.py`. Stop patches on b:
     `_build_backend` returns a fake whose async `delete_job` records ids;
     `_cancel_kubejob_log_stream`, `_reap_kubejob_console`,
     `_release_task_credential`, `_safe_emit_task_status` are no-ops, as in
     `tests/test_is_running_kubejob.py:58-67`.
   Traps: the ones above; route tests mark `route` in their names so step 2
   can deselect them with `-k "not route"`.

2. `apps/web-server/server/services/agent_kubejob.py:37-42` and `:902-903`:
   add `_LIVE_KINDS` after `_DISPATCH_GRACE_SECONDS = 45.0` (`:37`, before
   `_log` at `:42`); add `is_running_anywhere` (decision 3) immediately before
   `async def _kubejob_liveness` (`:903`) → verify by
   `python -m pytest tests/test_is_running_anywhere.py -q -k "not route"`
   (predicate rows pass), `ruff check apps/web-server/server/services/agent_kubejob.py`,
   `ruff format --check apps/web-server/server/services/agent_kubejob.py`.
   Traps: `sanitize_log` (`:21`) and `_log` (`:42`) already exist, add no
   import; no `exc_info`; leave `_kubejob_liveness` (`:903-972`),
   `_stop_kubejob_build` (`:986-`) and the reaper (`:871-873`) untouched.

3. `apps/web-server/server/routes/execution.py:260,275,966,991,1006,1074`
   and `tests/test_is_running_kubejob.py:250,253` (D1):
   - `:260` (`get_task_status`) and `:275` (`is_task_running`):
     `is_running = await agent_service.is_running_anywhere(task_id)`.
   - `:966` (`stop_task`): `if not await agent_service.is_running_anywhere(task_id):`;
     the 404 branch `:967-970` stays.
   - `:991`: `def _refuse_recovery_while_running` → `async def`.
   - `:1006`: `if force or not await agent_service.is_running_anywhere(task_id):`.
     Optionally update the docstring at `:999` that names `is_running`.
   - `:1074`: `await _refuse_recovery_while_running(task_id, agent_service, force=request.force)`.
     The comment at `:1068-1073` may stay.
   - D1: prefix both `execution._refuse_recovery_while_running(...)` calls at
     `tests/test_is_running_kubejob.py:250` and `:253` with `await`.
   → verify by the focused pytest command, the web-server suite, ruff and the
   diff guards in Tests.
   Traps: a missed `await` silently never raises (`:1074` is the only
   production caller); `:246-249` (`GET /running` list) and `:690` (Start)
   must not change; `routes/plan_approval.py`, `websockets/progress.py` and
   `charts/` must not change.

4. `CHANGELOG.md` (`## [Unreleased]`, existing `### Fixed` section) and this
   plan: add one entry and record D1/D2 as done, then commit → verify by
   `git add -A && python scripts/cq_ratchet.py --staged` and the focused
   pytest command once more.
   - Entry:
     > **A Kubernetes-Job build started by another web-server replica is now
     > seen as running (#1669).** Task status, the running badge, Stop and
     > Recover read the shared job-state store. A store read failure reads as
     > running, so Recover refuses rather than double-starting the build.
     > Start and plan approval are still pod-local; a follow-up issue tracks
     > them.
   - Commit (code, tests, CHANGELOG and plan in one commit):
     `fix(kubejob): read cross-replica job state for status, stop and recover (#1669)`.
   Traps: the PR description links intent, spec and plan, says which steps the
   coder did, and names the follow-ups: Start and plan approval; cross-replica
   subprocess builds plus marking orphaned subprocess rows terminal on
   startup; #1670; #1677.

Hand-off: three steps edit files and six files are touched, so once this plan
is approved it goes to the `coder` agent (step 1 first, later steps by
`SendMessage`), and a fresh Opus agent reviews the diff against this plan.

## Tests

Predicate tests in `tests/test_is_running_anywhere.py`, each calling
`await b.is_running_anywhere(TASK)`:

| Test | Expected |
|---|---|
| `test_k8s_job_row_from_other_replica_is_running` | True (B's sync `is_running` False) |
| `test_pending_row_is_running` | True |
| `test_subprocess_row_is_pod_local` | False |
| `test_queued_row_is_not_running` | False |
| `test_missing_row_is_not_running` | False |
| `test_terminal_row_is_not_running[done,failed,stuck,review]` | False |
| `test_store_error_reads_as_running` (b gets `_CountingStore`) | True; exactly one WARNING containing `#1669`, `record.exc_info is None` |
| `test_no_store_short_circuits` (`_store_enabled=False`, counting store, env still `kubejob`, `caplog` at WARNING) | False; `calls == []`; no WARNING record (`_kubejob_backend_enabled` warns when called without a store, so this is the only assertion that sees the `_store_enabled` half of the gate) |
| `test_subprocess_backend_short_circuits` (env `subprocess`, k8s-job row, b's store swapped to counting store) | False; `calls == []` |
| `test_local_is_running_short_circuits` (`b._active_kubejob_task_ids.add(TASK)`, counting store) | True; `calls == []` |

Route tests, all on B:

| Test | Expected |
|---|---|
| `test_route_status_and_running_see_other_replica` (k8s-job row) | `(await execution.get_task_status(TASK, _access={})).is_running is True`; `(await execution.is_task_running(TASK, _access={}))["is_running"] is True` |
| `test_route_recover_409_on_other_replica_build` | `HTTPException` 409, detail contains "still running" |
| `test_route_recover_409_on_store_error` | 409 |
| `test_route_recover_force_proceeds` | dict with `success` True |
| `test_route_stop_other_replica_kubejob` | `success` True; `deleted == [TASK]`; A's `get_state` says `failed`; then `await b.is_running_anywhere(TASK) is False` |
| `test_route_stop_404_without_row` | 404 |

Commands (worktree root, PATH exported):

```
python -m pytest tests/test_is_running_anywhere.py tests/test_recover_refuses_live_build.py tests/test_is_running_kubejob.py tests/test_kubejob_liveness.py tests/test_reap_abandoned_tasks.py tests/test_job_state_store.py tests/audit/test_task_action_audit.py tests/test_agent_service_durable_admission.py -q
python -m pytest tests -q
python -m pytest apps/web-server/tests -q -o asyncio_mode=auto
ruff check apps/backend apps/web-server scripts tests
ruff format --check apps/backend apps/web-server scripts tests
git add -A && python scripts/cq_ratchet.py --staged
python scripts/gen_autonomy_matrix.py --check
git diff --stat main
git diff main -- apps/web-server/server/routes/plan_approval.py charts/
git diff -U0 main -- apps/web-server/server/routes/execution.py | grep -E '^@@'
```

Expected:

- Focused command: the 80 baseline tests (measured at `15855ee3`) plus 19 new
  (10 predicate tests with the terminal one run 4 times = 13, plus 6 route
  tests), 0 failures.
- Full `tests` and `apps/web-server/tests`: green.
- ruff, ratchet and the autonomy-matrix check: clean (the matrix cites neither
  touched source file; the check is only a guard).
- `git diff --stat main`: only `agent_kubejob.py`, `execution.py`, the new
  test file, the three edited test files, `CHANGELOG.md` and `plan/`.
- `plan_approval.py` and `charts/`: empty diff.
- `execution.py` hunks only at `:260`, `:275`, `:966`, `:991`, `:999` (if
  edited), `:1006`, `:1074`; none at `:246-249` or `:690`.

Mutation checks (break one line, run `pytest tests/test_is_running_anywhere.py -q -x`,
restore with `git checkout -- <file>`):

| Break | Must fail |
|---|---|
| Delete `if self.is_running(...): return True` | `test_local_is_running_short_circuits` |
| Drop the `_store_enabled` half of the gate | `test_no_store_short_circuits` |
| Drop `or not self._kubejob_backend_enabled()` | `test_subprocess_backend_short_circuits` |
| `except` returns False | `test_store_error_reads_as_running`, `test_route_recover_409_on_store_error` |
| Add `exc_info=True` | `test_store_error_reads_as_running` |
| Remove the `lifecycle_state == "running"` clause | `test_terminal_row_is_not_running[*]` |
| `_LIVE_KINDS = ("k8s-job",)` | `test_pending_row_is_running` |
| Remove the kind clause, or add `"subprocess"` | `test_subprocess_row_is_pod_local` |
| `state is not None` → `state is None or` | `test_missing_row_is_not_running`, `test_route_stop_404_without_row` |
| `execution.py:260` or `:275` back to sync | `test_route_status_and_running_see_other_replica` |
| `:966` back to sync | `test_route_stop_other_replica_kubejob` |
| `:1006` back to sync | `test_route_recover_409_on_other_replica_build` |
| `await` dropped at `:1074` | `test_route_recover_409_on_other_replica_build`, `test_recover_refuses_while_the_build_runs` |
| `force or` dropped at `:1006` | `test_route_recover_force_proceeds`, `test_force_overrides_the_refusal` |

## Rollback

1. `git revert <sha>` of the single code commit and redeploy the previous
   image.
2. No migration, chart or data change exists, so the revert is complete.
   Behaviour falls back to each replica seeing only its own builds.
3. Code and tests live in one commit, so the revert restores the sync
   `_refuse_recovery_while_running` and the sync test calls together.
