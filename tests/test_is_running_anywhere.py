"""#1669 — a kubejob build dispatched by replica A reads as running on replica B.

Two ``AgentService`` instances share one SQLite job-state file, standing in for
two web-server replicas on one Postgres.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

_WEB_SERVER = Path(__file__).parent.parent / "apps" / "web-server"
if str(_WEB_SERVER) not in sys.path:
    sys.path.insert(0, str(_WEB_SERVER))
_BACKEND = Path(__file__).parent.parent / "apps" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from server.database.models import Base  # noqa: E402
from server.routes import execution  # noqa: E402
from server.services import job_state_store  # noqa: E402
from server.services.agent_service import AgentService  # noqa: E402

TASK = "proj-uuid:022-some-spec"
OTHER = "proj-uuid:023-other-spec"
K8S_REF = {"kind": "k8s-job", "job_name": "j", "namespace": "factory"}

_ENGINES: list = []


async def _make_factory(db_path: Path) -> async_sessionmaker:
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    _ENGINES.append(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def _dispose_engines():
    yield
    while _ENGINES:
        await _ENGINES.pop().dispose()


def _spawn(spec_id: str) -> job_state_store.SpawnArgs:
    return job_state_store.SpawnArgs(
        project_path="/tmp/p", spec_id=spec_id, parallel=True, workers=3
    )


class _CountingStore:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def get_state(self, job_id: str) -> Any:
        self.calls.append(job_id)
        raise RuntimeError("db down")


@pytest.fixture
async def pair(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AIFACTORY_BUILD_BACKEND", "kubejob")
    db = tmp_path / "jobs.db"
    f1 = await _make_factory(db)
    engine2 = create_async_engine(f"sqlite+aiosqlite:///{db}")
    _ENGINES.append(engine2)
    f2 = async_sessionmaker(engine2, expire_on_commit=False)
    a, b = AgentService(), AgentService()
    a._store_enabled = b._store_enabled = True
    a._job_store = job_state_store.JobStateStore(session_factory=f1)
    b._job_store = job_state_store.JobStateStore(session_factory=f2)
    return a, b


async def _row(a: AgentService, state: str, kind: str | None = None) -> None:
    store = a._job_store
    if state == "queued":
        await store.admit(OTHER, _spawn("other"), cap=1)
        await store.admit(TASK, _spawn("spec"), cap=1)
    elif state == "running" and kind == "pending":
        await store.admit(TASK, _spawn("spec"), cap=0)
    elif state == "running" and kind == "subprocess":
        await store.admit(TASK, _spawn("spec"), cap=0)
        await store.mark_running(TASK)
    else:
        await store.admit(TASK, _spawn("spec"), cap=0)
        await store.set_worker_ref(TASK, K8S_REF)
        if state != "running":
            await store.mark_terminal(TASK, state, error="x")


# -- predicate ---------------------------------------------------------------


async def test_k8s_job_row_from_other_replica_is_running(pair):
    a, b = pair
    await _row(a, "running", "k8s-job")
    assert b.is_running(TASK) is False
    assert await b.is_running_anywhere(TASK) is True


async def test_pending_row_is_running(pair):
    a, b = pair
    await _row(a, "running", "pending")
    assert b.is_running(TASK) is False
    assert await b.is_running_anywhere(TASK) is True


async def test_subprocess_row_is_pod_local(pair):
    a, b = pair
    await _row(a, "running", "subprocess")
    assert b.is_running(TASK) is False
    assert await b.is_running_anywhere(TASK) is False


async def test_queued_row_is_not_running(pair):
    a, b = pair
    await _row(a, "queued")
    assert b.is_running(TASK) is False
    assert await b.is_running_anywhere(TASK) is False


async def test_missing_row_is_not_running(pair):
    _, b = pair
    assert b.is_running(TASK) is False
    assert await b.is_running_anywhere(TASK) is False


@pytest.mark.parametrize("state", ["done", "failed", "stuck", "review"])
async def test_terminal_row_is_not_running(pair, state):
    a, b = pair
    await _row(a, state)
    assert b.is_running(TASK) is False
    assert await b.is_running_anywhere(TASK) is False


async def test_store_error_reads_as_running(pair, caplog):
    _, b = pair
    b._job_store = _CountingStore()
    with caplog.at_level(logging.WARNING):
        assert await b.is_running_anywhere(TASK) is True
    records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(records) == 1
    assert "#1669" in records[0].getMessage()
    assert records[0].exc_info is None


async def test_no_store_short_circuits(pair, caplog):
    _, b = pair
    store = b._job_store = _CountingStore()
    b._store_enabled = False
    with caplog.at_level(logging.WARNING):
        assert await b.is_running_anywhere(TASK) is False
    assert store.calls == []
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


async def test_subprocess_backend_short_circuits(pair, monkeypatch):
    a, b = pair
    await _row(a, "running", "k8s-job")
    monkeypatch.setenv("AIFACTORY_BUILD_BACKEND", "subprocess")
    store = b._job_store = _CountingStore()
    assert await b.is_running_anywhere(TASK) is False
    assert store.calls == []


async def test_local_is_running_short_circuits(pair):
    _, b = pair
    store = b._job_store = _CountingStore()
    b._active_kubejob_task_ids.add(TASK)
    assert await b.is_running_anywhere(TASK) is True
    assert store.calls == []


# -- routes (all on replica B) ----------------------------------------------


async def _noop(*_a: Any, **_k: Any) -> None:
    return None


@pytest.fixture
def routes(pair, monkeypatch, tmp_path):
    a, b = pair
    monkeypatch.setattr(execution, "get_agent_service", lambda: b)
    monkeypatch.setattr(execution, "emit_task_status", _noop)
    monkeypatch.setattr(execution, "audit_task_action", _noop)
    spec_dir = tmp_path / "proj" / ".aifactory" / "specs" / "022-some-spec"
    spec_dir.mkdir(parents=True)
    monkeypatch.setattr(
        execution,
        "load_projects",
        lambda: {"proj-uuid": {"path": str(tmp_path / "proj")}},
    )
    return a, b


async def test_route_status_and_running_see_other_replica(routes):
    a, _ = routes
    await _row(a, "running", "k8s-job")
    status = await execution.get_task_status(TASK, _access={})
    assert status.is_running is True
    assert (await execution.is_task_running(TASK, _access={}))["is_running"] is True


async def test_route_recover_409_on_other_replica_build(routes):
    a, _ = routes
    await _row(a, "running", "k8s-job")
    with pytest.raises(HTTPException) as caught:
        await execution.recover_task(
            TASK, execution.RecoverTaskRequest(), _access={"role": "member"}
        )
    assert caught.value.status_code == 409
    assert "still running" in str(caught.value.detail)


async def test_route_recover_409_on_store_error(routes):
    _, b = routes
    b._job_store = _CountingStore()
    with pytest.raises(HTTPException) as caught:
        await execution.recover_task(
            TASK, execution.RecoverTaskRequest(), _access={"role": "member"}
        )
    assert caught.value.status_code == 409


async def test_route_recover_force_proceeds(routes):
    a, _ = routes
    await _row(a, "running", "k8s-job")
    result = await execution.recover_task(
        TASK, execution.RecoverTaskRequest(force=True), _access={"role": "member"}
    )
    assert result["success"] is True


async def test_route_stop_other_replica_kubejob(routes, monkeypatch):
    a, b = routes
    await _row(a, "running", "k8s-job")
    deleted: list[str] = []

    class _Backend:
        async def delete_job(self, task_id: str) -> None:
            deleted.append(task_id)

    monkeypatch.setattr(b, "_build_backend", lambda: _Backend())
    monkeypatch.setattr(b, "_cancel_kubejob_log_stream", lambda *_a, **_k: None)
    monkeypatch.setattr(b, "_reap_kubejob_console", _noop)
    monkeypatch.setattr(b, "_release_task_credential", lambda *_a, **_k: None)
    monkeypatch.setattr(b, "_safe_emit_task_status", _noop)

    result = await execution.stop_task(TASK, _access={"role": "member"})
    assert result["success"] is True
    assert deleted == [TASK]
    state = await a._job_store.get_state(TASK)
    assert state["lifecycle_state"] == "failed"
    assert await b.is_running_anywhere(TASK) is False


async def test_route_stop_404_without_row(routes):
    with pytest.raises(HTTPException) as caught:
        await execution.stop_task(TASK, _access={"role": "member"})
    assert caught.value.status_code == 404
