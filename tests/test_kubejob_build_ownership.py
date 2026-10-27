"""#1704/#1705: Build ownership is shared and orphaned claims are reaped."""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

_ROOT = Path(__file__).resolve().parents[1]
for _path in (_ROOT / "apps" / "web-server", _ROOT / "apps" / "backend"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from server.database.models import Base, JobState  # noqa: E402
from server.routes import execution, plan_approval  # noqa: E402
from server.services import (  # noqa: E402
    agent_service,
    audit_service,
    build_backend,
    job_state_store,
    task_control,
)

TASK = "proj-uuid:022-some-spec"


@pytest.fixture
async def replicas(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFACTORY_BUILD_BACKEND", "kubejob")
    db = tmp_path / "jobs.db"
    engines = [create_async_engine(f"sqlite+aiosqlite:///{db}") for _ in range(2)]
    try:
        async with engines[0].begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        a, b = agent_service.AgentService(), agent_service.AgentService()
        for service, engine in zip((a, b), engines, strict=True):
            service._store_enabled = True
            service._job_store = job_state_store.JobStateStore(
                session_factory=async_sessionmaker(engine, expire_on_commit=False)
            )
        spec_dir = tmp_path / "proj" / ".aifactory" / "specs" / "022-some-spec"
        spec_dir.mkdir(parents=True)
        (spec_dir / "implementation_plan.json").write_text('{"phases": []}')
        (spec_dir / "review_state.json").write_text('{"approved": true}')
        task_control.record_plan_approval(spec_dir, "web_user")  # StartTask --force

        def projects():
            return {"proj-uuid": {"path": str(tmp_path / "proj")}}

        monkeypatch.setattr(execution, "load_projects", projects)
        monkeypatch.setattr(plan_approval, "load_projects", projects)
        monkeypatch.setattr(execution, "get_agent_service", lambda: b)
        monkeypatch.setattr(agent_service, "get_agent_service", lambda: b)
        monkeypatch.setattr(audit_service, "audit_task_action", AsyncMock())
        monkeypatch.setattr(execution, "emit_task_status", AsyncMock())
        yield a, b, spec_dir
    finally:
        for engine in engines:
            await engine.dispose()


async def _running_row(service):
    await service._job_store.admit(
        TASK,
        job_state_store.SpawnArgs(
            project_path="/tmp/p", spec_id="022-some-spec", parallel=True, workers=3
        ),
        cap=0,
    )
    await service._job_store.set_worker_ref(
        TASK, {"kind": "k8s-job", "job_name": "j", "namespace": "factory"}
    )


async def _start():
    return await execution.start_task(
        TASK,
        execution.StartTaskRequest(),
        SimpleNamespace(state=SimpleNamespace(user=None)),
        _access={"role": "member"},
    )


async def test_start_refuses_other_replica_build_without_stopping(
    replicas, monkeypatch
):
    a, b, _ = replicas
    await _running_row(a)
    stop = AsyncMock()
    monkeypatch.setattr(b, "stop_task", stop)

    with pytest.raises(HTTPException) as caught:
        await _start()
    assert caught.value.status_code == 409
    assert caught.value.detail == "Task is already running"
    stop.assert_not_awaited()


async def test_start_refuses_local_kubejob_without_stopping(replicas, monkeypatch):
    _, b, _ = replicas
    b._active_kubejob_task_ids.add(TASK)
    stop = AsyncMock()
    monkeypatch.setattr(b, "stop_task", stop)

    with pytest.raises(HTTPException) as caught:
        await _start()
    assert caught.value.status_code == 409
    stop.assert_not_awaited()


async def test_start_stops_only_registered_spec_process(replicas, monkeypatch):
    _, b, _ = replicas
    proc = object()
    b.running_tasks[TASK] = proc
    b._spec_creation_procs[TASK] = proc

    async def stop(task_id):
        assert task_id == TASK
        b.running_tasks.pop(task_id)

    stop_mock = AsyncMock(side_effect=stop)
    monkeypatch.setattr(b, "stop_task", stop_mock)
    monkeypatch.setattr(b, "start_task_execution", AsyncMock(return_value=None))

    result = await _start()
    assert result["success"] is True
    stop_mock.assert_awaited_once_with(TASK)
    assert TASK not in b._spec_creation_procs


async def test_start_keeps_other_replica_build_over_local_spec_process(
    replicas, monkeypatch
):
    a, b, _ = replicas
    await _running_row(a)
    proc = SimpleNamespace(pid=999999, wait=AsyncMock(return_value=0))
    b.running_tasks[TASK] = b._spec_creation_procs[TASK] = proc
    monkeypatch.setattr(b, "_signal_process_tree", lambda *_: None)
    for name in (
        "_update_plan_status",
        "_safe_emit_task_status",
        "_emit_progress",
        "start_task_execution",
    ):
        monkeypatch.setattr(b, name, AsyncMock())

    with pytest.raises(HTTPException) as caught:
        await _start()
    assert caught.value.status_code == 409
    b.start_task_execution.assert_not_awaited()
    state = await a._job_store.get_state(TASK)
    assert state["lifecycle_state"] == "running"


async def test_start_refuses_when_store_read_fails(replicas, monkeypatch):
    _, b, _ = replicas
    stop = AsyncMock()
    monkeypatch.setattr(b, "stop_task", stop)
    monkeypatch.setattr(
        b._job_store, "get_state", AsyncMock(side_effect=RuntimeError("db down"))
    )

    with pytest.raises(HTTPException) as caught:
        await _start()
    assert caught.value.status_code == 409
    stop.assert_not_awaited()


async def test_approval_refuses_build_before_writing_approval(replicas, monkeypatch):
    a, b, spec_dir = replicas
    (spec_dir / "review_state.json").write_text('{"approved": false}')
    await _running_row(a)
    stop = AsyncMock()
    monkeypatch.setattr(b, "stop_task", stop)

    with pytest.raises(HTTPException) as caught:
        await plan_approval.approve_plan(
            TASK,
            plan_approval.ApprovePlanRequest(auto_restart=True),
            _access={"role": "member"},
        )
    assert caught.value.status_code == 409
    assert json.loads((spec_dir / "review_state.json").read_text())["approved"] is False
    stop.assert_not_awaited()


async def test_subprocess_claim_stamps_owner_and_does_not_resurrect_failed_row(
    replicas,
):
    a, _, _ = replicas
    store = a._job_store
    await store.admit(TASK, job_state_store.SpawnArgs("/tmp/p", "022-some-spec"), cap=0)
    await store.mark_running(TASK)
    state = await store.get_state(TASK)
    assert state["worker_ref"] == {
        "kind": "subprocess",
        "pod": job_state_store.POD_NAME,
        "boot": job_state_store.BOOT_ID,
    }

    await store.mark_terminal(TASK, "failed", error="stopped")
    await store.mark_running(TASK)
    state = await store.get_state(TASK)
    assert state["lifecycle_state"] == "failed"
    assert state["worker_ref"]["boot"] == job_state_store.BOOT_ID
    assert state["error"] == "stopped"


async def test_running_subprocess_rows_skip_other_workers_and_pending(replicas):
    a, _, _ = replicas
    store = a._job_store
    args = job_state_store.SpawnArgs("/tmp/p", "022-some-spec")
    for job_id in ("subprocess", "legacy", "k8s", "pending"):
        await store.admit(job_id, args, cap=0)
    await store.mark_running("subprocess")
    await store.set_worker_ref("legacy", {"kind": "subprocess"})
    await store.set_worker_ref("k8s", {"kind": "k8s-job", "job_name": "j"})

    rows = await store.get_running_subprocess_rows()
    assert sorted(rows, key=lambda row: row["job_id"]) == [
        {"job_id": "legacy", "pod": None, "boot": None},
        {
            "job_id": "subprocess",
            "pod": job_state_store.POD_NAME,
            "boot": job_state_store.BOOT_ID,
        },
    ]


async def test_fail_subprocess_row_checks_exact_owner_and_running_state(replicas):
    a, _, _ = replicas
    store = a._job_store
    await store.admit(TASK, job_state_store.SpawnArgs("/tmp/p", "022-some-spec"), cap=0)
    await store.mark_running(TASK)
    pod, boot = job_state_store.POD_NAME, job_state_store.BOOT_ID

    assert not await store.fail_subprocess_row(TASK, pod, "other-boot", "orphan")
    assert not await store.fail_subprocess_row(TASK, "other-pod", boot, "orphan")
    assert (await store.get_state(TASK))["lifecycle_state"] == "running"
    assert await store.fail_subprocess_row(TASK, pod, boot, "orphan")
    assert not await store.fail_subprocess_row(TASK, pod, boot, "again")
    state = await store.get_state(TASK)
    assert state["lifecycle_state"] == "failed"
    assert state["error"] == "orphan"
    async with store._session_factory() as session:
        row = await session.get(JobState, TASK)
        assert row.ended_at is not None


async def test_subprocess_claim_precedes_spawn_and_is_skipped_without_store(
    monkeypatch,
):
    service = agent_service.AgentService()
    service._store_enabled = True
    monkeypatch.setenv("AIFACTORY_BUILD_BACKEND", "subprocess")
    calls = []

    async def mark_running(job_id):
        calls.append(("claim", job_id))

    async def spawn(**kwargs):
        calls.append(("spawn", kwargs["task_id"]))
        return "process"

    service._job_store = SimpleNamespace(mark_running=mark_running)
    monkeypatch.setattr(service, "_spawn_task_execution", spawn)
    assert (
        await service._start_build_unit(
            task_id=TASK, project_path=Path("/tmp/p"), spec_id="022-some-spec"
        )
        == "process"
    )
    assert calls == [("claim", TASK), ("spawn", TASK)]

    service._store_enabled = False
    calls.clear()
    assert (
        await service._start_build_unit(
            task_id=TASK, project_path=Path("/tmp/p"), spec_id="022-some-spec"
        )
        == "process"
    )
    assert calls == [("spawn", TASK)]


async def _subprocess_row(service, *, pod, boot):
    store = service._job_store
    await store.admit(TASK, job_state_store.SpawnArgs("/tmp/p", "022-some-spec"), cap=0)
    await store.set_worker_ref(TASK, {"kind": "subprocess", "pod": pod, "boot": boot})


async def test_startup_reaps_previous_boot_before_counting(replicas, monkeypatch):
    a, b, _ = replicas
    monkeypatch.setattr(agent_service, "_own_namespace", lambda: None)
    await _subprocess_row(a, pod=job_state_store.POD_NAME, boot="old-boot")

    assert await b.reconcile_on_startup() == {"running": 0, "queued": 0}
    state = await a._job_store.get_state(TASK)
    assert state["lifecycle_state"] == "failed"
    assert state["error"] == "control plane restarted under the build (#1705)"


@pytest.mark.parametrize("boot", [job_state_store.BOOT_ID, None])
async def test_startup_leaves_current_or_unstamped_row(replicas, monkeypatch, boot):
    a, b, _ = replicas
    monkeypatch.setattr(agent_service, "_own_namespace", lambda: None)
    await _subprocess_row(a, pod=job_state_store.POD_NAME if boot else None, boot=boot)

    assert await b.reconcile_on_startup() == {"running": 1, "queued": 0}
    assert (await a._job_store.get_state(TASK))["lifecycle_state"] == "running"


class _PodError(Exception):
    def __init__(self, status):
        self.status = status


@pytest.mark.parametrize(
    "error", [_PodError(404), _PodError(403), _PodError(500), TimeoutError()]
)
async def test_other_pod_is_reaped_only_on_404(replicas, monkeypatch, error, caplog):
    a, b, _ = replicas
    monkeypatch.setattr(agent_service, "_own_namespace", lambda: "factory")
    await _subprocess_row(a, pod="gone-pod", boot="old-boot")
    core = SimpleNamespace(read_namespaced_pod=AsyncMock(side_effect=error))

    reaped = await b.reap_orphaned_subprocess_rows(core=core)
    assert reaped == ([TASK] if getattr(error, "status", None) == 404 else [])
    state = await a._job_store.get_state(TASK)
    assert state["lifecycle_state"] == ("failed" if reaped else "running")
    if reaped:
        assert state["error"] == "owning pod gone-pod no longer exists (#1705)"
    else:
        assert f"status={getattr(error, 'status', None)}" in caplog.text
    core.read_namespaced_pod.assert_awaited_once_with("gone-pod", "factory")


async def test_no_namespace_never_reads_other_pod(replicas, monkeypatch):
    a, b, _ = replicas
    monkeypatch.setattr(agent_service, "_own_namespace", lambda: None)
    await _subprocess_row(a, pod="other-pod", boot="old-boot")
    core = SimpleNamespace(read_namespaced_pod=AsyncMock())

    assert await b.reap_orphaned_subprocess_rows(core=core) == []
    core.read_namespaced_pod.assert_not_awaited()
    assert (await a._job_store.get_state(TASK))["lifecycle_state"] == "running"


async def test_reaper_does_not_fail_reclaimed_row(replicas, monkeypatch):
    a, b, _ = replicas
    monkeypatch.setattr(agent_service, "_own_namespace", lambda: None)
    await _subprocess_row(a, pod=job_state_store.POD_NAME, boot="old-boot")
    fail = b._job_store.fail_subprocess_row

    async def restamp_then_fail(job_id, pod, boot, reason):
        assert (pod, boot) == (job_state_store.POD_NAME, "old-boot")
        await a._job_store.set_worker_ref(
            job_id, {"kind": "subprocess", "pod": pod, "boot": "new-boot"}
        )
        return await fail(job_id, pod, boot, reason)

    monkeypatch.setattr(b._job_store, "fail_subprocess_row", restamp_then_fail)
    assert await b.reap_orphaned_subprocess_rows() == []
    state = await a._job_store.get_state(TASK)
    assert state["lifecycle_state"] == "running"
    assert state["worker_ref"]["boot"] == "new-boot"


class _FakeBatch:
    def __init__(self, jobs=(), *, list_error=None, delete_error=None):
        self.jobs = jobs
        self.list_error = list_error
        self.delete_error = delete_error
        self.listed = []
        self.deleted = []

    async def list_namespaced_job(self, namespace, *, label_selector):
        self.listed.append((namespace, label_selector))
        if self.list_error:
            raise self.list_error
        return SimpleNamespace(items=self.jobs)

    async def delete_namespaced_job(self, name, namespace, *, propagation_policy):
        self.deleted.append((name, namespace, propagation_policy))
        if self.delete_error:
            raise self.delete_error


def _job(name, created):
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, creation_timestamp=created)
    )


@pytest.mark.parametrize("setting", ["false", None, "flase"])
async def test_sweep_dry_run_and_single_delete(monkeypatch, setting):
    if setting is None:
        monkeypatch.delenv("AIFACTORY_JOB_SWEEP_DRY_RUN", raising=False)
    else:
        monkeypatch.setenv("AIFACTORY_JOB_SWEEP_DRY_RUN", setting)
    now = datetime.now(UTC)
    batch = _FakeBatch([_job("orphan", now - timedelta(seconds=601))])

    store = SimpleNamespace(get_active_kubejobs=AsyncMock(return_value=[]))
    assert await build_backend.KubeJobBuildBackend(store).sweep_orphan_jobs(
        now=now.timestamp(), batch=batch
    ) == ["orphan"]
    assert batch.listed == [("factory", "app=aifactory,factory.io/kind=task")]
    assert batch.deleted == (
        [("orphan", "factory", "Background")] if setting == "false" else []
    )
    assert not hasattr(batch, "delete_collection_namespaced_job")


async def test_sweep_keeps_young_and_owned_jobs(monkeypatch):
    monkeypatch.setenv("AIFACTORY_JOB_SWEEP_DRY_RUN", "false")
    pending_id = "proj-uuid:023-pending"
    store = SimpleNamespace(
        get_active_kubejobs=AsyncMock(
            return_value=[
                {"job_id": TASK, "job_name": "j"},
                {"job_id": pending_id, "job_name": None},
            ]
        )
    )
    pending_name = build_backend._reconstructed_ref(pending_id)[0]
    now = datetime.now(UTC)
    old = now - timedelta(seconds=601)
    batch = _FakeBatch(
        [
            _job("young", now - timedelta(seconds=599)),
            _job("j", old),
            _job(pending_name, old),
            _job("orphan", old),
        ]
    )

    assert await build_backend.KubeJobBuildBackend(store).sweep_orphan_jobs(
        now=now.timestamp(), batch=batch
    ) == ["orphan"]
    assert batch.deleted == [("orphan", "factory", "Background")]


async def test_sweep_store_or_list_failure_deletes_nothing(monkeypatch):
    monkeypatch.setenv("AIFACTORY_JOB_SWEEP_DRY_RUN", "false")
    old = datetime.now(UTC) - timedelta(seconds=601)
    batch = _FakeBatch([_job("orphan", old)])
    store = SimpleNamespace(
        get_active_kubejobs=AsyncMock(side_effect=RuntimeError("db"))
    )
    backend = build_backend.KubeJobBuildBackend(store)
    assert await backend.sweep_orphan_jobs(batch=batch) == []
    assert batch.listed == batch.deleted == []

    store.get_active_kubejobs = AsyncMock(return_value=[])
    batch.list_error = RuntimeError("api")
    assert await backend.sweep_orphan_jobs(batch=batch) == []
    assert batch.deleted == []


async def test_sweep_404_counts_as_deleted_but_other_delete_errors_do_not(
    monkeypatch,
):
    monkeypatch.setenv("AIFACTORY_JOB_SWEEP_DRY_RUN", "false")
    old = datetime.now(UTC) - timedelta(seconds=601)
    store = SimpleNamespace(get_active_kubejobs=AsyncMock(return_value=[]))
    backend = build_backend.KubeJobBuildBackend(store)

    for status, expected in [(404, ["orphan"]), (403, [])]:
        batch = _FakeBatch([_job("orphan", old)], delete_error=_PodError(status))
        assert await backend.sweep_orphan_jobs(batch=batch) == expected
        assert batch.deleted == [("orphan", "factory", "Background")]


@pytest.mark.parametrize("reaper_error", [False, True])
async def test_reap_kubejob_builds_sweeps_even_without_reaped_rows(
    monkeypatch, reaper_error
):
    service = agent_service.AgentService()
    monkeypatch.setenv("AIFACTORY_BUILD_BACKEND", "kubejob")
    service._store_enabled = True
    reap = AsyncMock(
        side_effect=RuntimeError("reap") if reaper_error else None, return_value=[]
    )
    sweep = AsyncMock()
    monkeypatch.setattr(
        service,
        "_build_backend",
        lambda: SimpleNamespace(reap_vanished_jobs=reap, sweep_orphan_jobs=sweep),
    )

    assert await service.reap_kubejob_builds() == []
    sweep.assert_awaited_once_with()


async def test_dispatch_cancelled_holds_credential_and_propagates(
    tmp_path, monkeypatch
):
    service = agent_service.AgentService()
    monkeypatch.setattr(service, "_write_skill_context", lambda _path: None)
    monkeypatch.setattr(
        service, "_resolve_claude_token_pooled", lambda _task: ("token", "id", "name")
    )
    released = []
    monkeypatch.setattr(service, "_release_task_credential", released.append)
    monkeypatch.setattr(
        service,
        "_build_backend",
        lambda: SimpleNamespace(dispatch=AsyncMock(side_effect=asyncio.CancelledError)),
    )

    with pytest.raises(asyncio.CancelledError):
        await service._dispatch_build_job(
            task_id=TASK,
            project_path=tmp_path,
            spec_id="022-some-spec",
            correlation_key=None,
        )
    assert service._held_credentials == {TASK}
    assert released == []

    # A later dispatch that succeeds owns the credential: the hold is dropped
    # so a tick before its Job exists cannot free the live build's token.
    service._build_backend = lambda: SimpleNamespace(dispatch=AsyncMock())
    await service._dispatch_build_job(
        task_id=TASK,
        project_path=tmp_path,
        spec_id="022-some-spec",
        correlation_key=None,
    )
    assert service._held_credentials == set()
    assert released == []


@pytest.mark.parametrize("outcome", [True, False, RuntimeError("api")])
async def test_held_credential_waits_for_job_404(monkeypatch, outcome):
    monkeypatch.setenv("AIFACTORY_BUILD_BACKEND", "kubejob")
    service = agent_service.AgentService()
    service._store_enabled = True
    service._held_credentials.add(TASK)
    released = []
    monkeypatch.setattr(service, "_release_task_credential", released.append)
    gone = AsyncMock(
        side_effect=outcome if isinstance(outcome, Exception) else None,
        return_value=outcome,
    )
    backend = SimpleNamespace(
        reap_vanished_jobs=AsyncMock(return_value=[]),
        job_gone=gone,
        sweep_orphan_jobs=AsyncMock(),
    )
    monkeypatch.setattr(service, "_build_backend", lambda: backend)

    await service.reap_kubejob_builds()
    assert service._held_credentials == (set() if outcome is True else {TASK})
    assert released == ([TASK] if outcome is True else [])
    if outcome is True:
        await service.reap_kubejob_builds()
        assert released == [TASK]
        gone.assert_awaited_once_with(TASK)


@pytest.mark.parametrize("status,expected", [(404, True), (200, False), (500, False)])
async def test_job_gone_only_on_404(status, expected):
    class Batch:
        async def read_namespaced_job(self, name, namespace):
            assert (name, namespace) == build_backend._reconstructed_ref(TASK)
            if status != 200:
                raise _PodError(status)
            return SimpleNamespace(status=SimpleNamespace())

    backend = build_backend.KubeJobBuildBackend(SimpleNamespace())
    assert await backend.job_gone(TASK, batch=Batch()) is expected
