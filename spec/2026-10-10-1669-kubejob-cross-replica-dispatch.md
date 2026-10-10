---
status: draft
issue: 1669
intent: intent/2026-10-10-1669-kubejob-cross-replica-dispatch.md
---

# Spec: every replica sees a kubejob build as running, not only the one that dispatched it

## Design

Paths are relative to `apps/web-server/server/` unless they start with `tests/`
or `charts/`.

One async predicate, `is_running_anywhere`, sits beside the sync `is_running`.
Recover, Stop and the two status endpoints (the badge) read it. Nothing else
changes: no new store code, no chart change.

### Decisions on the intent's open questions

Each is the default taken for this spec. Two of them (Q4, Q6) are narrowed
where the literal answer would break the approved outcome "single-replica
installs behave exactly as today"; the narrowing is marked **[narrowed]**.

1. **Q1 Scope.** Recover, Stop, `GET /{id}/status` and `GET /{id}/running`
   only. Start (`routes/execution.py:690`) and plan approval
   (`routes/plan_approval.py:170`) keep the pod-local `is_running` and get a
   follow-up issue.
2. **Q2 Timing.** Fix now. Any operator who sets `replicaCount > 1`
   (`charts/aifactory/values.yaml:32`) or enables the HPA (`:191-194`) with
   rmux off is exposed today.
3. **Q3 Cost.** One primary-key read per status poll, and only when the
   in-memory answer is False and the kubejob backend is on
   (`_kubejob_backend_enabled`, `services/agent_kubejob.py:104-124`, which
   already requires the store). The default subprocess backend never reads the
   store here, so it behaves exactly as today even with `DATABASE_URL` set.
4. **Q4 Store read failure.** Fail closed: an exception reads as running.
   Recover returns 409, Stop proceeds to `stop_task`, status shows running.
   A missing row or a terminal row (`services/build_backend.py:355`) reads as
   not running. **[narrowed]** A `queued` row also reads as not running, and
   so does a `running` row whose `worker_ref.kind` is `subprocess`. See Q6 and
   Risk 1 for why.
5. **Q5 Shape.** The sync `AgentService.is_running`
   (`services/agent_service.py:1560-1569`) is unchanged. The new async method
   lives in `services/agent_kubejob.py` next to `_kubejob_liveness` (`:903`),
   following the reaper's in-memory-then-store order (`:871-873`).
   `_refuse_recovery_while_running` becomes async and is awaited at
   `routes/execution.py:1074`, outside `recover_task`'s branch budget.
6. **Q6 Subprocess builds on another replica.** Out of scope, with a
   follow-up issue. **[narrowed]** The predicate counts a `running` row only
   when its `worker_ref.kind` is `k8s-job` or `pending` (a granted slot not
   yet claimed, #1606), and reads the store only when the kubejob backend is
   on (Q3). The approved answer said "regardless of backend", but an in-pod
   build's row is never marked terminal if its pod dies mid-build:
   `reconcile_on_startup` (`services/agent_service.py:1525-1558`) only counts
   and drains. On the subprocess backend that row keeps the `pending` kind
   `admit` stamped (`services/job_state_store.py:262,275`) for its whole life:
   `mark_running`, which would stamp `subprocess` (`:299`), has no caller, and
   only the kubejob backend calls `set_worker_ref`
   (`services/build_backend.py:1294,1419`). Every chart install sets
   `DATABASE_URL`, so `_store_enabled` is on by default, and with the backend
   default of subprocess a single-replica pod restart would leave the card
   "running" forever, Recover refusing with 409 and no UI path to `force`.
   The kind filter alone does not prevent that, because the row is `pending`;
   the backend gate does. The kind filter still drops `subprocess` rows on a
   kubejob install. Kubejob is what the intent is about, so the two conditions
   cost nothing it promised. On the kubejob backend an orphaned `pending` row
   is already reaped (`get_active_kubejobs` includes it,
   `services/job_state_store.py:483`).
7. **Q7 Sequencing.** Separate PRs: #1669, then #1670 (the log streamer
   reuses this predicate in place of the grace logic at
   `services/agent_kubejob.py:625-646`), then #1677.
8. **Q8 Verification.** A unit test with two `AgentService` instances on two
   `JobStateStore`s over one aiosqlite file, following
   `tests/test_job_state_store.py:255-265`. See Verification.
9. **Q9 Plan approval and approved `/start` stopping a live Job.** Out of this
   change. Both keep the pod-local `is_running`. Moving them to the new
   predicate would make every replica delete live Jobs
   (`routes/plan_approval.py:170-176`, `routes/execution.py:690-701`,
   `services/agent_service.py:1385-1389`); the follow-up issue decides.
10. **Q10 Chart comment.** No chart change. The replica pin stays, and naming
    #1669 as a precondition would suggest it is enough on its own.

### 1. The predicate, `services/agent_kubejob.py`

Added just before `_kubejob_liveness` (`:903`). `_TERMINAL_STATES` is not
needed: anything other than a live kubejob-kind `running` row is "not
running", which covers missing, terminal and `queued` at once.

```python
_LIVE_KINDS = ("k8s-job", "pending")  # subprocess rows: pod-local, see Q6

async def is_running_anywhere(self, task_id: str) -> bool:
    """#1669: is_running() plus the durable row, so a kubejob build another
    replica dispatched counts. Recover/Stop/status only; Start and plan
    approval stay pod-local. A store error reads as running (#1551)."""
    if self.is_running(task_id):
        return True
    if not getattr(self, "_store_enabled", False) or not self._kubejob_backend_enabled():
        return False  # no store or subprocess backend: is_running() is the full answer
    try:
        state = await self._store().get_state(task_id)
    except Exception:  # noqa: BLE001 - doubt must never read as "not running"
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

- No `exc_info` on the warning: status polls during a DB outage would flood the
  log with tracebacks.
- `_store_enabled` is checked first so a kubejob-without-store misconfig does
  not log `_kubejob_backend_enabled`'s warning on every status poll.
- `_kubejob_liveness` is left alone; it serves the reaper with a different
  meaning for a missing row.

### 2. Call sites, `routes/execution.py`

- `:260` (`GET /{id}/status`) and `:275` (`GET /{id}/running`, read by the
  badge via `apps/frontend-web/src/lib/api-adapter.ts:397`):
  `is_running = await agent_service.is_running_anywhere(task_id)`.
- `:966` (Stop): `if not await agent_service.is_running_anywhere(task_id):` 404.
  From there `stop_task` already works across replicas: it calls
  `_stop_kubejob_build` (`services/agent_service.py:1385-1389`), which reads
  the store and deletes the Job (`services/agent_kubejob.py:986-1011`).
- `:991` `_refuse_recovery_while_running` becomes `async def`; `:1006` becomes
  `if force or not await agent_service.is_running_anywhere(task_id):`;
  `:1074` becomes `await _refuse_recovery_while_running(...)`.

Unchanged: `:690` (Start), `routes/plan_approval.py:170`,
`websockets/progress.py`, `GET /running` list (`get_running_tasks`, `:248`),
the reaper, and `charts/`.

### 3. Test fakes that must gain the method

- `tests/test_recover_refuses_live_build.py:35-42` `_Agent`: add
  `async def is_running_anywhere(self, _t): return self._running`.
- `tests/audit/test_task_action_audit.py:96` `SimpleNamespace`: add
  `is_running_anywhere=AsyncMock(return_value=True)`.

Without these, the routes raise `AttributeError`.

## Alternatives rejected

1. **Make `AgentService.is_running` async, or have it read the store.** It
   ripples into every sync caller, including Start, plan approval and the
   reaper (`services/agent_kubejob.py:871`), and would make Start and plan
   approval delete live Jobs on every replica (Q1, Q5, Q9).
2. **Reuse `_kubejob_liveness`.** It maps a missing row to "unknown"
   (`:950-955`), which would show every never-built task as running, and its
   "no store means absent" relies on the caller's prior check.
3. **`JobStateStore.is_running` (`services/job_state_store.py:518-528`).**
   Smallest, but it counts a dead pod's in-pod build row as running (Risk 1)
   and lets exceptions escape. `get_state` plus the kind check is the same one
   PK read.
4. **Count every non-terminal row, including `queued` (Q4's literal
   wording).** With the store on by default, a queued task would turn the
   badge to running and refuse Recover on a single replica, which today it
   does not. A queued row is not a build that Recover could clobber.
5. **Inline `try/await store` in each route.** Four copies of the fail-closed
   logic is the badge-versus-refusal drift #1619 warns about
   (`routes/execution.py:999-1003`).
6. **Read timeout (`asyncio.wait_for`) around the store call.** The engine has
   no statement timeout (`database/engine.py:61-66`), but every other store
   read in the service has the same exposure; a timeout belongs on the engine,
   not on one predicate. Noted in Risks.
7. **Stop-specific 409 when `stop_task` fails off-pod.** Two cases remain: a
   store outage, and a `pending` row on a replica that did not dispatch it
   (`_stop_kubejob_build` stops only `k8s-job` rows,
   `services/agent_kubejob.py:1004-1005`). Both are short-lived and a 500 is
   honest; see Risk 8.
8. **Cache the store answer per task.** It brings back the window this issue
   closes.

## Risks

1. **Narrowing Q4/Q6 needs confirmation.** If the reviewer wants another
   pod's in-pod build or a queued row to count, drop the backend gate and kind
   filter or widen the state check, and accept that a single-replica pod
   restart leaves a subprocess task (its row stuck `running`/`pending`)
   "running" with no UI recovery. The follow-up issue
   for cross-replica subprocess builds should also cover marking orphaned
   subprocess rows terminal on startup.
2. **Store outage turns cards "running".** Recover returns 409 and Stop
   returns 500 (`_stop_kubejob_build` re-reads and fails, `:993-1001`) for the
   outage's duration. Fail-closed by design (Q4); it clears on its own.
3. **No read timeout.** A stalled DB makes a status poll wait for the driver's
   own timeout. Same exposure as every other store read; engine-level fix if it
   ever bites.
4. **Polling cost.** One PK read per poll for each card that is not running in
   this pod's memory, only with the store on (Q3).
5. **Async conversion.** A missed `await` on `_refuse_recovery_while_running`
   leaves an unawaited coroutine that never raises. Its only caller is
   `:1074`; test (b) below fails if it is missed.
6. **Pod-local reads remain.** `progress.py` heartbeats, `GET /running`,
   Start and plan approval keep pod-local answers. All go in the follow-up
   issue.
7. **Single-replica, store on, kubejob, right after a restart.** A live
   `k8s-job` row now reads as running before the first reconcile tick fills
   the in-memory set. That is the correct answer and closes the same window on
   one replica.
8. **Stop on a `pending` row from another replica returns 500.** The row is
   counted live (the slot is granted), but `_stop_kubejob_build` returns False
   for any kind other than `k8s-job` (`services/agent_kubejob.py:1002-1005`), so
   `stop_task` returns False and the route answers 500 "Failed to stop task"
   until `set_worker_ref` stamps `k8s-job`. Today that replica answers 404, so
   neither stops the build; the retry succeeds once the Job is recorded. The
   window is #1677's, which is sequenced after this.

## Verification

New file `tests/test_is_running_anywhere.py`: two `AgentService` instances,
each with `_store_enabled = True` and its own `JobStateStore` over one aiosqlite
file (`tests/test_job_state_store.py:43,255-265` pattern). Service A admits the
task; service B checks:

| Row state on A | B: `is_running_anywhere` | B: routes |
|---|---|---|
| `running`, `kind: k8s-job` | True (B's sync `is_running` False) | `/status` and `/running` say `is_running: true`; Recover 409; `force=True` proceeds |
| `running`, `kind: pending` | True | |
| `running`, `kind: subprocess` | False | |
| `queued` | False | |
| missing | False | |
| each of `done`, `failed`, `stuck`, `review` | False | |
| `get_state` raises | True | Recover 409 |
| `_store_enabled = False`, store raises if called | False, store never called | |

Both services run with `AIFACTORY_BUILD_BACKEND=kubejob` (monkeypatched env);
two more rows cover the gate:

| Setup | B: `is_running_anywhere` |
|---|---|
| backend `subprocess`, store on, row `running` `k8s-job`, store raises if called | False, store never called |
| backend `kubejob`, B's sync `is_running` True | True, store never called |

Stop on B with a `running` `k8s-job` row: the route returns 200 and
`delete_job` (faked) is called once; with no row it returns 404.

Existing tests stay green: #1619, #1662, #1551, #1001 and the durable store.

```
cd /mnt/code/Source-home/GitHub/AIFactory-1669
pytest tests/test_is_running_anywhere.py tests/test_recover_refuses_live_build.py \
       tests/test_is_running_kubejob.py tests/test_kubejob_liveness.py \
       tests/test_reap_abandoned_tasks.py tests/test_job_state_store.py \
       tests/audit/test_task_action_audit.py tests/test_agent_service_durable_admission.py -q
ruff check apps/web-server/server/routes/execution.py apps/web-server/server/services/agent_kubejob.py
```

Done means all of the above pass, and the diff touches only `agent_kubejob.py`
(one constant, one method), `routes/execution.py` (the four call sites plus
`async` on `:991` and `await` at `:1074`), one new test file and two fake
updates. `routes/execution.py:690`, `routes/plan_approval.py:170` and
`charts/` show no diff.
