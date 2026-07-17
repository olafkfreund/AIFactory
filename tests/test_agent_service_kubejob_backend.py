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
from server.services.job_state_store import JobStateStore, SpawnArgs  # noqa: E402

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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
        task_id="p:s1",
        project_path=tmp_path,
        spec_id="s1",
    )

    assert proc is None  # no in-pod Process for a Job-backed build
    assert dispatched == ["p:s1"]
    assert spawned == []  # subprocess path NOT taken
    # The durable row is running under the shared cap.
    assert await service._store().is_running("p:s1") is True


async def test_default_backend_still_spawns_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
        task_id="p:s2",
        project_path=tmp_path,
        spec_id="s2",
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


async def test_dispatch_build_job_forwards_pooled_oauth_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #671 OAuth-env defect: _dispatch_build_job resolves the build's credential
    # from the SAME pool the in-pod path uses and forwards it to the backend's
    # dispatch (which injects it into the Job container env). A fresh Job pod has
    # no credential source, so this is the load-bearing fix.
    service = await _make_service(tmp_path / "tok.db")

    monkeypatch.setattr(
        service,
        "_resolve_claude_token_pooled",
        lambda _tid: ("pooled-tok", "prof-1", "Profile One"),
    )

    captured: dict[str, Any] = {}

    class _FakeBackend:
        async def dispatch(self, **kw: Any) -> str:
            captured.update(kw)
            return "factory-aifactory-job"

    monkeypatch.setattr(service, "_build_backend", lambda: _FakeBackend())
    monkeypatch.setattr(
        service,
        "_start_kubejob_log_stream",
        lambda **_kw: _noop(),
    )
    monkeypatch.setattr(service, "_safe_emit_task_status", _noop_status)

    await service._dispatch_build_job(
        task_id="p:s1",
        project_path=tmp_path,
        spec_id="s1",
        correlation_key="9",
    )

    assert captured["oauth_token"] == "pooled-tok"
    assert captured["task_id"] == "p:s1"


async def test_dispatch_build_job_releases_token_on_dispatch_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # If the Job dispatch raises, the pooled credential is returned immediately
    # (the Job will never run, so a reaper would never fire to release it).
    service = await _make_service(tmp_path / "tokerr.db")

    monkeypatch.setattr(
        service,
        "_resolve_claude_token_pooled",
        lambda _tid: ("pooled-tok", "prof-1", "Profile One"),
    )
    released: list[str] = []
    monkeypatch.setattr(
        service, "_release_task_credential", lambda tid: released.append(tid)
    )

    class _BoomBackend:
        async def dispatch(self, **_kw: Any) -> str:
            raise RuntimeError("cluster down")

    monkeypatch.setattr(service, "_build_backend", lambda: _BoomBackend())

    with pytest.raises(RuntimeError, match="cluster down"):
        await service._dispatch_build_job(
            task_id="p:s1",
            project_path=tmp_path,
            spec_id="s1",
            correlation_key="9",
        )
    assert released == ["p:s1"]


async def _noop() -> None:
    return None


async def _noop_status(*_a: Any, **_kw: Any) -> None:
    return None


async def test_drain_promotes_queued_build_via_kubejob_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regression for the drain-path gap (live-found on the build-default flip): a
    # build promoted from the admission queue MUST honour
    # AIFACTORY_BUILD_BACKEND=kubejob (dispatch a Job), not silently run in-pod.
    # _drain_queue_durable used to call _spawn_task_execution directly, so the
    # flip was a no-op for every build that went through admission/queueing.
    monkeypatch.setenv("AIFACTORY_BUILD_BACKEND", "kubejob")
    service = await _make_service(tmp_path / "drain.db")
    service.settings.MAX_CONCURRENT_TASKS = 1

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

    # Fill the single slot, then queue a second build.
    a = await service._store().admit(
        "p:a", SpawnArgs(project_path=str(tmp_path), spec_id="a"), 1
    )
    b = await service._store().admit(
        "p:b", SpawnArgs(project_path=str(tmp_path), spec_id="b"), 1
    )
    assert a == "started" and b == "queued"

    # Free the slot + drain → the queued build is promoted. It MUST dispatch a
    # Job (kubejob), not run in-pod.
    await service._store().mark_terminal("p:a", "done")
    await service._drain_queue()

    assert dispatched == ["p:b"]  # promoted build dispatched a Job
    assert spawned == []  # NOT the in-pod subprocess (the bug)


async def test_start_build_unit_forwards_parallel_opts_to_kubejob_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #914: _start_build_unit accepted parallel/workers and forwarded them ONLY
    # on the subprocess branch -- the kubejob branch silently dropped them, so
    # the #376 wave harness never reached run.py on the live default backend.
    service = await _make_service(tmp_path / "par.db")
    monkeypatch.setattr(service, "_kubejob_backend_enabled", lambda: True)

    captured: dict[str, Any] = {}

    async def fake_dispatch(**kw: Any) -> None:
        captured.update(kw)

    monkeypatch.setattr(service, "_dispatch_build_job", fake_dispatch)

    await service._start_build_unit(
        task_id="p:s1",
        project_path=tmp_path,
        spec_id="s1",
        correlation_key="9",
        parallel=True,
        workers=4,
    )

    assert captured["parallel"] is True
    assert captured["workers"] == 4


async def test_start_build_unit_forwards_mode_and_base_branch_to_kubejob_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #916 remainder: same defect class as #914/#915 — _start_build_unit accepted
    # mode/base_branch and forwarded them ONLY on the subprocess branch, so a
    # quick-mode task ran the full pipeline in the Job and a base-branch override
    # was silently ignored.
    service = await _make_service(tmp_path / "qm.db")
    monkeypatch.setattr(service, "_kubejob_backend_enabled", lambda: True)

    captured: dict[str, Any] = {}

    async def fake_dispatch(**kw: Any) -> None:
        captured.update(kw)

    monkeypatch.setattr(service, "_dispatch_build_job", fake_dispatch)

    await service._start_build_unit(
        task_id="p:s1",
        project_path=tmp_path,
        spec_id="s1",
        correlation_key="9",
        base_branch="release/2.0",
        mode="quick",
    )

    assert captured["mode"] == "quick"
    assert captured["base_branch"] == "release/2.0"


async def test_dispatch_build_job_writes_skill_context_before_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #916 remainder: selectedSkills were never materialized for kubejob builds.
    # The in-pod path writes skill_context.md into the authored spec dir before
    # spawning; the kubejob path must do the same BEFORE dispatch (the backend's
    # worktree population copies the spec dir into the Job's /work, so ordering
    # is what makes the file travel).
    service = await _make_service(tmp_path / "sk.db")

    order: list[str] = []

    monkeypatch.setattr(
        service,
        "_resolve_claude_token_pooled",
        lambda _tid: (None, None, None),
    )
    monkeypatch.setattr(
        service,
        "_write_skill_context",
        lambda spec_dir: order.append(f"skills:{spec_dir}"),
    )

    class _FakeBackend:
        async def dispatch(self, **_kw: Any) -> str:
            order.append("dispatch")
            return "factory-aifactory-job"

    monkeypatch.setattr(service, "_build_backend", lambda: _FakeBackend())
    monkeypatch.setattr(service, "_start_kubejob_log_stream", lambda **_kw: _noop())
    monkeypatch.setattr(service, "_safe_emit_task_status", _noop_status)

    await service._dispatch_build_job(
        task_id="p:s1",
        project_path=tmp_path,
        spec_id="s1",
        correlation_key="9",
    )

    expected_spec_dir = tmp_path / ".aifactory" / "specs" / "s1"
    assert order == [f"skills:{expected_spec_dir}", "dispatch"]
