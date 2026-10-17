"""#1704: Start and approval preserve builds owned by any replica."""

from __future__ import annotations

import json
import sys
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

from server.database.models import Base  # noqa: E402
from server.routes import execution, plan_approval  # noqa: E402
from server.services import agent_service, audit_service, job_state_store  # noqa: E402

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
