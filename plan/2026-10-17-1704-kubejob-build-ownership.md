---
status: approved
issue: 1704
spec: spec/2026-10-17-1704-kubejob-build-ownership.md
---

# Plan: one owner per build, visible from every replica

Self-contained. You do not need to open the intent or the spec. Covers
#1704, #1705 and #1714. Paths are under `apps/web-server/server/` unless they
start with `tests/` (repo root). Line numbers are against this branch after the
merge of `origin/dev` (`1dc5440d`).

## Approved decisions (Option A, chosen by the user 2026-10-17)

The `job_states` row is the only owner of a build. Kubernetes is evidence,
never an order. Any doubt (store read error, Kubernetes API error, a row
without an owner stamp) reads as "live, leave it alone".

1. **#1704 Start and plan approval never stop a build.** They return 409 while
   `is_running_anywhere(task_id)` is true, including when the store read fails
   (it already reads a failure as running). The only thing they may stop is a
   local spec-creation process, identified by object identity of the process
   that `start_spec_creation` registered. Plan approval checks this only when
   `auto_restart` is set (without it nothing starts), and BEFORE it writes the
   approval, so a 409 leaves the plan unapproved.
2. **#1705 Subprocess rows carry their owner.** `mark_running` stamps
   `worker_ref = {"kind": "subprocess", "pod": POD_NAME, "boot": BOOT_ID}`
   where `POD_NAME = $HOSTNAME` (or `socket.gethostname()`) and `BOOT_ID` is a
   uuid made once per process. It is called on the subprocess path BEFORE the
   process is spawned (today nothing calls it, so subprocess rows keep `pending`
   for life). A row is failed when: its pod is this pod and its boot is not this
   boot (at startup and every tick), or its pod is another pod and
   `read_namespaced_pod` returns 404 (anything else: leave it). The fail is a
   compare-and-set on the exact `(pod, boot)` read, so a re-admitted row is
   never failed. Rows without a stamp are left alone (forced Recover clears
   them, as today). Then `is_running_anywhere` counts every `running` row,
   whatever its kind, on both backends whenever the store is enabled.
3. **#1714 Job sweep, dry-run first.** Each kubejob reaper tick lists Jobs in
   the build namespace with selector `app=aifactory,factory.io/kind=task` and,
   one at a time with `delete_namespaced_job` (never `delete_collection`),
   deletes a Job only when it is older than 600 s AND no `running` row owns it
   (recorded `job_name`, or the reconstructed name for a `pending` row) AND the
   store read and the list both succeeded this tick. The switch is
   `AIFACTORY_JOB_SWEEP_DRY_RUN`, parsed like `stale_reaper.dry_run()`
   (`services/stale_reaper.py:64-71`): anything but an explicit `false` only
   logs "would delete". The sweep runs in the control plane, so the switch is
   not forwarded into build Jobs.
4. **Credential.** `_dispatch_build_job` catches `BaseException`. A raise from
   dispatch does not prove no Job exists (the create may have landed before a
   timeout, or the rollback delete failed), so the credential is never released
   in the except block. The task id goes into a pod-local
   `_held_credentials` set, and each reaper tick releases it once
   `read_namespaced_job` on the reconstructed Job name returns 404. When dispatch
   failed before any Job, that happens on the next tick (about 15 s).
   Deviation from the spec text (it proposed a dedicated exception carrying the
   Job name): the Job name is deterministic (`_reconstructed_ref`), so no new
   exception type is needed, and "create raised" is not proof of absence.
5. No migration, no new RBAC, no chart change. If `list` Jobs or `get` Pods is
   refused (403), log it and skip; report it, never add RBAC.
6. The reconcile loop also starts on the subprocess backend when the store is
   enabled, so the pod-404 rule runs there. Its kubejob-only calls are already
   no-ops off the kubejob backend; `reap_abandoned_tasks` is gated on
   `kubejob_enabled()` so its behaviour on the subprocess backend is unchanged
   (it never ran there).

Known limits (accepted): the sweep cannot tell apart two AIFactory releases
that dispatch into the same namespace (both label `app=aifactory`); dry-run is
the guard until an operator flips it. A row's task status in the spec dir is
not changed by the #1705 reaper on the subprocess backend.

## Steps

All tests go in ONE new file `tests/test_kubejob_build_ownership.py` (style:
`tests/test_is_running_anywhere.py`: SQLite `JobStateStore` via
`async_sessionmaker`, two `AgentService` instances sharing one DB file,
`_FakeBatch`-style fakes as in `tests/test_build_backend_kubejob.py:61-102`).
Run tests from the repo root with
`/mnt/code/Source-home/GitHub/AIFactory/apps/backend/.venv/bin/pytest`.

### Step 1 (#1704): Start and approval refuse instead of stopping

- `services/agent_service.py:107` (in `__init__`, beside
  `_active_kubejob_task_ids`): add
  `self._spec_creation_procs: dict[str, Any] = {}` with a one-line comment
  (#1704: the spec-creation process per task, so Start/approve stop only that).
- `services/agent_spec_creation.py:299`: after
  `self.running_tasks[task_id] = proc`, add
  `self._spec_creation_procs[task_id] = proc`; declare
  `_spec_creation_procs: dict[str, Any]` in the `TYPE_CHECKING` block at
  `:51-60`.
- `services/agent_service.py`, new method right before `stop_task` (`:1343`):
  `async def stop_stale_spec_creation(self, task_id: str) -> None` - if
  `self.running_tasks.get(task_id)` is not None and `is` the object in
  `self._spec_creation_procs.get(task_id)`, call `await self.stop_task(task_id)`
  (on any `Exception`, log a warning and `self.running_tasks.pop(task_id, None)`,
  the fallback both routes have today), then pop `_spec_creation_procs[task_id]`.
  Otherwise do nothing. Never touches the store or a Job.
- `routes/execution.py:699-715`: replace the block with
  ```python
  if force_execution:
      await agent_service.stop_stale_spec_creation(task_id)
  if await agent_service.is_running_anywhere(task_id):
      raise HTTPException(status_code=409, detail="Task is already running")
  ```
  (use `status.HTTP_409_CONFLICT` as the file does). Keep the detail text.
- `routes/plan_approval.py`: delete the stale-process block at `:166-184`
  (the `if agent_service.is_running(task_id): ... pop` lines). Insert before
  `review_state = ReviewState.load(spec_dir)` (`:110`):
  ```python
  if request.auto_restart:
      from ..services.agent_service import get_agent_service
      agent_service = get_agent_service()
      await agent_service.stop_stale_spec_creation(task_id)
      if await agent_service.is_running_anywhere(task_id):
          raise HTTPException(status_code=409, detail="Task is already running (possibly on another replica)")
  ```
  The later `agent_service = get_agent_service()` inside the auto-restart `try`
  stays.
- Tests (fail before the fix): (a) Start with an approved plan
  (`review_state.json` `{"approved": true}`) while replica A holds a running
  `k8s-job` row: replica B's `execution.start_task` raises 409 and a patched
  `stop_task` is never called. Reuse the route fixture pattern of
  `tests/test_is_running_anywhere.py:182-197` and look at how
  `execution.start_task` is called in existing tests
  (`grep -rn "start_task(" tests/`). (b) Same with replica A itself having the
  task in `_active_kubejob_task_ids`: 409, no `stop_task`. (c) A local spec
  process (fake proc registered in both `running_tasks` and
  `_spec_creation_procs`, no row): `stop_task` is called once and the request
  is not 409 (patch `start_task_execution` to a no-op). (d) Store read raises:
  409. (e) `approve_plan` with `auto_restart=True` over a running `k8s-job`
  row: 409 and `review_state.json` still not approved.
- Check: `pytest tests/test_kubejob_build_ownership.py tests/test_is_running_anywhere.py -q`
- Traps: `stop_task` on a non-local task reaches `_stop_kubejob_build`, which
  deletes a Job; the routes must not call it except through
  `stop_stale_spec_creation`. `HTTPException` raised inside the approval's
  `try/except Exception` at `:200-213` would be swallowed; that is why the
  check goes before the approval.

### Step 2 (#1705): stamp subprocess rows with pod and boot

- `services/job_state_store.py:23-46`: `import socket`, `import uuid`; add
  module constants
  `POD_NAME = os.environ.get("HOSTNAME", "").strip() or socket.gethostname()`
  and `BOOT_ID = uuid.uuid4().hex`, with a comment (#1705: on Kubernetes
  HOSTNAME is the pod name; BOOT_ID changes on every container restart).
- `mark_running` (`:283-302`): return early unless the row exists AND
  `lifecycle_state == "running"` (never resurrect a row a stop or exit already
  closed); stamp `{"kind": "subprocess", "pod": POD_NAME, "boot": BOOT_ID}`.
  Keep the phase/admission writes. Update the docstring.
- New `async def get_running_subprocess_rows(self) -> list[dict[str, Any]]`
  next to `get_active_kubejobs` (`:445`): `running` rows of this `SERVICE`
  whose `worker_ref.kind == "subprocess"`, as
  `{"job_id", "pod", "boot"}` (`pod`/`boot` may be None for legacy rows).
- New `async def fail_subprocess_row(self, job_id: str, pod: str, boot: str, error: str) -> bool`:
  in one transaction, `session.get` the row; only if it is `running` and its
  `worker_ref` has `kind == "subprocess"`, the same `pod` and the same `boot`,
  set `lifecycle_state="failed"`, `error`, `ended_at=_now()` and return True;
  else False.
- `services/agent_kubejob.py:483-536` `_start_build_unit`: in the subprocess
  branch, before `return await self._spawn_task_execution(...)`, add
  `if getattr(self, "_store_enabled", False): await self._store().mark_running(task_id)`
  with a comment (#1705: claim before spawn, so a crash in between is still
  this pod's row; a raise here is a spawn failure and the callers free the
  slot).
- Tests: `mark_running` stamps pod and boot; does nothing to a `failed` row;
  `get_running_subprocess_rows` skips k8s-job/pending rows;
  `fail_subprocess_row` returns False for a different boot, a different pod, a
  non-running row, and True + `failed` for the match; `_start_build_unit` on the
  subprocess backend calls `mark_running` before `_spawn_task_execution`
  (patch both, record order) and not at all with `_store_enabled=False`.
- Check: `pytest tests/test_kubejob_build_ownership.py tests/test_reap_unclaimed_slots.py tests/test_job_state_store.py -q`
- Traps: `tests/test_reap_unclaimed_slots.py:125,146` call `mark_running`
  after `admit(cap=0)` (row is `running`), so the new guard keeps them green;
  run them. Never stamp in `admit` or `drain`: `pending` must keep meaning
  "nobody claimed this" (#1606, comment at `job_state_store.py:36-44`).

### Step 3 (#1705): reap orphaned subprocess rows; count them everywhere

- `services/agent_service.py`: module-level
  `_SA_NAMESPACE_FILE = Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace")`
  and `def _own_namespace() -> str | None` (read + strip, `None` on `OSError`
  or empty). New method after `reconcile_on_startup` (`:1517-1550`):
  `async def reap_orphaned_subprocess_rows(self, *, core: Any = None) -> list[str]`:
  - return `[]` unless `self._store_enabled`; read
    `get_running_subprocess_rows()` (on error: warning, return `[]`).
  - for each row: skip when `pod` or `boot` is falsy. Same pod
    (`== job_state_store.POD_NAME`): fail when `boot != BOOT_ID`, reason
    `"control plane restarted under the build (#1705)"`. Other pod: fail only
    when the pod read returns 404, reason
    `f"owning pod {pod} no longer exists (#1705)"`.
  - pod read: `ns = _own_namespace()`; if None, treat as "not gone" (not in a
    cluster: never guess). Build the client once per call, only if needed:
    `kubernetes_asyncio.config.load_incluster_config()` then
    `client.CoreV1Api(client.ApiClient())` (NO kubeconfig fallback: a dev
    kubeconfig points at some other cluster); close `api_client` in `finally`
    when this call built it. `await core.read_namespaced_pod(pod, ns)`: success
    = alive; exception with `status == 404` = gone; any other exception =
    alive, log a warning naming the status (a 403 means the Pods `get` RBAC is
    missing: report it).
  - fail with `fail_subprocess_row(job_id, pod, boot, reason)`; collect ids
    where it returned True; errors are logged, never raised.
  - if anything was failed, `await self._drain_queue()`. Return the ids.
- `reconcile_on_startup`: call `await self.reap_orphaned_subprocess_rows()`
  right after the store-enabled guard, before `reconstruct()` (so the counts
  and the drain see the freed slots).
- `services/agent_kubejob.py:863-893` `kubejob_reconcile_loop`: each tick, after
  `reap_kubejob_builds()`, `await self.reap_orphaned_subprocess_rows()`; gate
  `reap_abandoned_tasks()` with `kubejob_enabled()` (import from
  `.build_backend`, already imported there as module) so it still runs only
  where it ran before. Declare `reap_orphaned_subprocess_rows` in the mixin's
  `TYPE_CHECKING` block (`:113-136`).
- `main.py:236-250`: start the loop when
  `_kubejob_enabled() or store_enabled()` (import `store_enabled` from
  `.services.job_state_store`); adjust the comment and log line.
- `services/agent_kubejob.py:32` and `:1002-1028` `is_running_anywhere`: delete
  `_LIVE_KINDS`; drop the `not self._kubejob_backend_enabled()` condition (keep
  the `_store_enabled` one); return
  `state is not None and state.get("lifecycle_state") == "running"`. Update the
  docstring (#1705: every running row is live; orphaned subprocess rows are
  closed by the reaper above).
- `tests/test_is_running_anywhere.py`: `test_subprocess_row_is_pod_local`
  (`:113-117`) now expects True (rename `test_subprocess_row_is_running`);
  `test_subprocess_backend_short_circuits` (`:155-161`) becomes
  `test_subprocess_backend_reads_store`: replica A's running row reads True on
  B with the subprocess backend. These flips are the intended behaviour.
- New tests: same pod + old boot → failed by `reconcile_on_startup`; same pod +
  same boot → untouched; unstamped row → untouched; other pod with fake core
  raising `status=404` → failed; 403, 500 and a plain `TimeoutError` →
  untouched; `_own_namespace` returning None → untouched and the fake core is
  never called; row re-stamped by another boot between read and fail → not
  failed (call `fail_subprocess_row` with stale values).
- Check: `pytest tests/test_kubejob_build_ownership.py tests/test_is_running_anywhere.py tests/test_recover_refuses_live_build.py tests/test_agent_kubejob_mixin.py tests/test_is_running_kubejob.py -q`
- Traps: `main.py` loop start must not change anything on the kubejob backend.
  Monkeypatch `_own_namespace` in tests; never read the real SA file.
  `_kubejob_liveness` (`:1030`) already treats any running row as live: leave
  it.

### Step 4 (#1714): sweep Jobs no row owns, dry-run by default

- `services/build_backend.py` near `_ENV_DEADLINE` (`:156`):
  `_ENV_SWEEP_DRY_RUN = "AIFACTORY_JOB_SWEEP_DRY_RUN"`,
  `_SWEEP_GRACE_SECONDS = 600`,
  `_SWEEP_SELECTOR = "app=aifactory,factory.io/kind=task"`, and
  `def sweep_dry_run() -> bool` returning
  `os.environ.get(_ENV_SWEEP_DRY_RUN, "true").strip().lower() != "false"`
  (one-line comment: fails closed on a typo, like `stale_reaper.dry_run`).
- New `KubeJobBuildBackend.sweep_orphan_jobs(self, *, now: float | None = None, batch: Any = None) -> list[str]`
  after `reap_vanished_jobs` (`:1476-1561`):
  - `rows = await self._store.get_active_kubejobs()`; on error log and return
    `[]`. `owned = {r["job_name"] or _reconstructed_ref(r["job_id"])[0] for r in rows}`.
  - `namespace = _dispatch_namespace()`; client as in `reap_vanished_jobs`
    (owns/closes it). `listing = await batch.list_namespaced_job(namespace, label_selector=_SWEEP_SELECTOR)`;
    on error log a warning (name 403 as missing `list` RBAC) and return `[]`.
  - for each `job in listing.items or []`: `name = job.metadata.name`;
    `created = _to_epoch(job.metadata.creation_timestamp)`; skip when no name,
    `name in owned`, `created is None`, or `clock - created < _SWEEP_GRACE_SECONDS`.
    Dry-run: `_log.warning("[build_backend] #1714 dry-run: would delete orphan Job %s/%s ...")`,
    append, continue. Else `delete_namespaced_job(name, namespace, propagation_policy="Background")`;
    a 404 counts as done; any other error is logged and the Job is skipped.
    Append deleted names. Return the list.
- `services/agent_kubejob.py:895-920` `reap_kubejob_builds`: after the reaper
  block (and also when `reaped` is empty), in its own `try/except Exception`,
  `await self._build_backend().sweep_orphan_jobs()`. Must not change the
  reaper's return value.
- Tests (extend a `_FakeBatch` with `list_namespaced_job` returning
  `SimpleNamespace(items=[...])` with `metadata.name`,
  `metadata.creation_timestamp` as an aware `datetime`): orphan older than
  grace is deleted exactly once with env `false`; with env unset or `flase`
  nothing is deleted and the name is returned; younger than grace kept; Job
  named by a running row kept; Job matching a `pending` row's reconstructed
  name kept; store read raising → no list, no delete; list raising → no delete;
  the fake has no `delete_collection_namespaced_job` attribute use (assert the
  fake records only single deletes).
- Check: `pytest tests/test_kubejob_build_ownership.py tests/test_build_backend_kubejob.py -q`
- Traps: never `delete_collection_namespaced_job` (verb not granted). Use
  `_to_epoch` (`:1708`) for the timestamp. The label `factory.io/job-id` holds
  only the last 20 characters of the id, so match by Job NAME from the rows,
  never by id.

### Step 5 (#1714): hold the credential until the Job is gone

- `services/agent_service.py:107` `__init__`: `self._held_credentials: set[str] = set()`
  (comment: #1714 task ids whose dispatch raised; released when their Job 404s).
  Declare it in the `agent_kubejob.py` mixin `TYPE_CHECKING` block.
- `services/agent_kubejob.py:455-460` `_dispatch_build_job`: change
  `except Exception:` to `except BaseException:`; replace the release with
  `self._held_credentials.add(task_id)` and a comment saying why (a raise does
  not prove no Job exists). Keep `raise`.
- `services/build_backend.py`: new `async def job_gone(self, job_id: str, batch: Any = None) -> bool`
  on `KubeJobBuildBackend`: `job_name, namespace = _reconstructed_ref(job_id)`,
  owns/closes the client like `delete_job`, returns
  `await self._job_outcome(batch, namespace, job_name) == "gone"` (any non-404
  error reads as present, already true of `_job_outcome`).
- `services/agent_kubejob.py` `reap_kubejob_builds`: before the sweep, for each
  `task_id in list(self._held_credentials)`: in `try`, if
  `await self._build_backend().job_gone(task_id)`, call
  `self._release_task_credential(task_id)` and `discard` it; on error log and
  keep it.
- `tests/test_agent_service_kubejob_backend.py:185-216`
  (`test_dispatch_build_job_releases_token_on_dispatch_error`): now assert the
  credential is NOT released at the raise and the id is in
  `_held_credentials`; rename to `..._holds_token_on_dispatch_error`.
- New tests: dispatch raising `RuntimeError` → held, not released; raising
  `asyncio.CancelledError` → held, the `CancelledError` propagates; a tick with
  `job_gone` True → released once and the set is empty; `job_gone` False or
  raising → still held; `job_gone` on a fake batch: 404 → True, present → False,
  500 → False.
- Check: `pytest tests/test_kubejob_build_ownership.py tests/test_agent_service_kubejob_backend.py tests/test_build_backend_kubejob.py -q`
- Traps: `CancelledError` must still propagate (re-raise). The pool is per pod
  and in memory; nothing is persisted.
- Deviation (implementation): after a successful dispatch,
  `self._held_credentials.discard(task_id)`. The pool's checkout is idempotent
  per task id, so a retried dispatch reuses the held credential; without the
  discard a tick between that checkout and the new Job's create would read 404
  and free the live build's credential. Tested in
  `test_dispatch_cancelled_holds_credential_and_propagates`.

## Tests

From the repo root, with `PATH=/mnt/code/Source-home/GitHub/AIFactory/apps/backend/.venv/bin:$PATH`:

- `pytest tests/test_kubejob_build_ownership.py tests/test_is_running_anywhere.py tests/test_recover_refuses_live_build.py tests/test_agent_kubejob_mixin.py tests/test_is_running_kubejob.py tests/test_reap_unclaimed_slots.py tests/test_job_state_store.py tests/test_build_backend_kubejob.py tests/test_agent_service_kubejob_backend.py tests/test_agent_service_durable_admission.py -q` → all pass.
- `pytest tests/ -m "not slow" -q -x -k "kubejob or job_state or plan_approval or start_task or reap"` → pass.
- `ruff check` and `ruff format --check` on every touched file.
- Each new test fails on `origin/dev` (prove with a detached scratch worktree).

## Rollback

Revert the PR. No schema or chart change. Rows stamped with `pod`/`boot`
remain valid JSON that the old code reads as plain `subprocess` rows. To stop
the sweep without a revert, unset `AIFACTORY_JOB_SWEEP_DRY_RUN` (dry-run is the
default).
