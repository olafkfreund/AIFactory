"""RFC-0016 #671 — AgentService routes builds by backend (subprocess|kubejob).

Checks the control/execution split at the AgentService seam: with the durable
store enabled and ``AIFACTORY_BUILD_BACKEND=kubejob``, ``start_task_execution``
dispatches a k8s Job (not an in-pod subprocess) and admits through the SAME
durable cap. With the default backend it still spawns a subprocess. The k8s
client and the actual Job dispatch are stubbed — no cluster needed.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

_ROOT = Path(__file__).resolve().parents[1] / "apps" / "web-server"
if str(_ROOT) not in sys.path:
    sys.path.append(str(_ROOT))

from server.database.models import Base  # noqa: E402
from server.services.agent_service import AgentService  # noqa: E402
from server.services.job_state_store import JobStateStore  # noqa: E402

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


async def _make_service(db_path: Path) -> AgentService:
    service = AgentService()
    service.settings.MAX_CONCURRENT_TASKS = 2
    service._store_enabled = True
    service._job_store = await _make_store(db_path)
    return service


async def test_kubejob_backend_dispatches_job_not_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIFACTORY_BUILD_BACKEND", "kubejob")
    service = await _make_service(tmp_path / "kj.db")

    spawned: list[str] = []
    dispatched: list[str] = []

    async def fake_spawn(*, task_id: str, **_kw: Any) -> Any:
        spawned.append(task_id)

    async def fake_dispatch(*, task_id: str, **_kw: Any) -> None:
        dispatched.append(task_id)

    async def fake_status(*_a: Any, **_kw: Any) -> None:
        return None

    monkeypatch.setattr(service, "_spawn_task_execution", fake_spawn)
    monkeypatch.setattr(service, "_dispatch_build_job", fake_dispatch)
    monkeypatch.setattr(service, "_safe_emit_task_status", fake_status)

    proc = await service.start_task_execution(
        task_id="p:s1", project_path=tmp_path, spec_id="s1",
    )

    assert proc is None  # no in-pod Process for a Job-backed build
    assert dispatched == ["p:s1"]
    assert spawned == []  # subprocess path NOT taken
    # The durable row is running under the shared cap.
    assert await service._store().is_running("p:s1") is True


async def test_default_backend_still_spawns_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AIFACTORY_BUILD_BACKEND", raising=False)
    service = await _make_service(tmp_path / "sp.db")

    spawned: list[str] = []
    dispatched: list[str] = []

    async def fake_spawn(*, task_id: str, **_kw: Any) -> Any:
        spawned.append(task_id)
        service.running_tasks[task_id] = cast(Any, object())

    async def fake_dispatch(*, task_id: str, **_kw: Any) -> None:
        dispatched.append(task_id)

    monkeypatch.setattr(service, "_spawn_task_execution", fake_spawn)
    monkeypatch.setattr(service, "_dispatch_build_job", fake_dispatch)

    await service.start_task_execution(
        task_id="p:s2", project_path=tmp_path, spec_id="s2",
    )
    assert spawned == ["p:s2"]
    assert dispatched == []


async def test_kubejob_falls_back_to_subprocess_without_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # kubejob requested but no durable store → fall back (no reconcile loop).
    monkeypatch.setenv("AIFACTORY_BUILD_BACKEND", "kubejob")
    service = AgentService()
    service._store_enabled = False
    assert service._kubejob_backend_enabled() is False
