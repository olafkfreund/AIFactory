"""AgentService durable-admission wiring tests (RFC-0016 #668) — fast lane.

These check that AgentService routes the cap/queue through the durable store
when it is enabled, against a real SQLite file DB (no Postgres needed), with
the actual subprocess spawn stubbed. They complement:

* ``test_agent_service_admission.py`` — the in-memory fallback path
  (unchanged behaviour, no DATABASE_URL); and
* ``test_job_state_store.py`` — the store in isolation.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

PROJECT_ROOT = Path(__file__).resolve().parents[1] / "apps" / "web-server"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.append(str(PROJECT_ROOT))

from server.database.models import Base  # noqa: E402
from server.services.agent_service import AgentService  # noqa: E402
from server.services.job_state_store import JobStateStore  # noqa: E402


class _FakeProc:
    def __init__(self, task_id: str) -> None:
        self.task_id = task_id


_ENGINES: list = []


@pytest.fixture(autouse=True)
async def _dispose_engines():
    yield
    while _ENGINES:
        await _ENGINES.pop().dispose()


async def _make_store(db_path: Path) -> JobStateStore:
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    _ENGINES.append(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    return JobStateStore(session_factory=factory)


async def _make_service(
    cap: int, db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[AgentService, list[str]]:
    service = AgentService()
    service.settings.MAX_CONCURRENT_TASKS = cap
    # Force the durable path on, with a SQLite-backed store.
    service._store_enabled = True
    service._job_store = await _make_store(db_path)

    spawned: list[str] = []

    async def fake_spawn(*, task_id: str, **_kwargs: Any) -> _FakeProc:
        proc = _FakeProc(task_id)
        service.running_tasks[task_id] = cast(Any, proc)
        spawned.append(task_id)
        return proc

    async def fake_mark_queued(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(service, "_spawn_task_execution", fake_spawn)
    monkeypatch.setattr(service, "_mark_task_queued", fake_mark_queued)
    return service, spawned


async def _start(service: AgentService, task_id: str, tmp_path: Path) -> Any:
    return await service.start_task_execution(
        task_id=task_id,
        project_path=tmp_path,
        spec_id=task_id.rsplit(":", 1)[-1],
    )


async def test_durable_under_cap_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, spawned = await _make_service(2, tmp_path / "a.db", monkeypatch)
    proc = await _start(service, "p:001", tmp_path)
    assert proc is not None
    assert spawned == ["p:001"]
    assert await service._store().is_running("p:001")


async def test_durable_at_cap_queues_not_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, spawned = await _make_service(2, tmp_path / "b.db", monkeypatch)
    await _start(service, "p:001", tmp_path)
    await _start(service, "p:002", tmp_path)
    proc = await _start(service, "p:003", tmp_path)
    assert proc is None  # queued, not started
    assert "p:003" not in spawned
    assert await service._store().is_queued("p:003")


async def test_durable_same_running_task_409(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _ = await _make_service(5, tmp_path / "c.db", monkeypatch)
    await _start(service, "p:001", tmp_path)
    with pytest.raises(ValueError, match="already running"):
        await _start(service, "p:001", tmp_path)


async def test_durable_drain_promotes_fifo_on_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, spawned = await _make_service(1, tmp_path / "d.db", monkeypatch)
    await _start(service, "p:001", tmp_path)  # running
    await _start(service, "p:002", tmp_path)  # queued
    await _start(service, "p:003", tmp_path)  # queued

    # Simulate the running build finishing: monitor drops it from
    # running_tasks, frees the durable slot, then drains.
    service.running_tasks.pop("p:001", None)
    await service._free_durable_slot_on_exit("p:001")
    await service._drain_queue()

    assert "p:002" in spawned
    assert service.is_running("p:002")
    assert await service._store().get_queued_job_ids() == ["p:003"]


async def test_durable_stop_removes_queued(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _ = await _make_service(1, tmp_path / "e.db", monkeypatch)
    await _start(service, "p:001", tmp_path)  # running
    await _start(service, "p:002", tmp_path)  # queued
    stopped = await service.stop_task("p:002")
    assert stopped is True
    assert await service._store().get_queued_job_ids() == []


async def test_durable_reconcile_on_startup_drains_dead_replica_slots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Replica 1 admits 1 running + 1 queued, then "dies": its running build's
    # row is left as-is, but it never re-spawns here.
    db = tmp_path / "f.db"
    service1, _ = await _make_service(1, db, monkeypatch)
    await _start(service1, "p:001", tmp_path)  # running
    await _start(service1, "p:002", tmp_path)  # queued

    # Replica 2 boots fresh over the same DB. The previously-running build's
    # owner is gone; reconcile should see 1 running + 1 queued and, since the
    # cap is 1 and there is still a running row, NOT over-admit.
    service2 = AgentService()
    service2.settings.MAX_CONCURRENT_TASKS = 1
    service2._store_enabled = True
    service2._job_store = JobStateStore(
        session_factory=service1._job_store._session_factory
    )
    spawned2: list[str] = []

    async def fake_spawn2(*, task_id: str, **_kwargs: Any) -> _FakeProc:
        proc = _FakeProc(task_id)
        service2.running_tasks[task_id] = cast(Any, proc)
        spawned2.append(task_id)
        return proc

    monkeypatch.setattr(service2, "_spawn_task_execution", fake_spawn2)
    counts = await service2.reconcile_on_startup()
    assert counts == {"running": 1, "queued": 1}
    # Cap=1 with a still-running row -> queued p:002 stays queued.
    assert spawned2 == []
    assert await service2._store().get_queued_job_ids() == ["p:002"]
