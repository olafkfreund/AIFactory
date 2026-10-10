"""RFC-0016 #671 — AgentService routes builds by backend (subprocess|kubejob).

Checks the control/execution split at the AgentService seam: with the durable
store enabled and ``AIFACTORY_BUILD_BACKEND=kubejob``, ``start_task_execution``
dispatches a k8s Job (not an in-pod subprocess) and admits through the SAME
durable cap. With the default backend it still spawns a subprocess. The k8s
client and the actual Job dispatch are stubbed — no cluster needed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

_ROOT = Path(__file__).resolve().parents[1] / "apps" / "web-server"
if str(_ROOT) not in sys.path:
    sys.path.append(str(_ROOT))
_BACKEND = Path(__file__).resolve().parents[1] / "apps" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from core import artifact_store as a_s  # noqa: E402
from core import workspace_fetch as wf  # noqa: E402
from server.database.models import Base  # noqa: E402
from server.services import (  # noqa: E402
    agent_kubejob,
    completion,
    completion_orchestration,
    task_control,
)
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


# ---------------------------------------------------------------------------
# #1633 C2 + C4: failed/stopped/reaped builds report their spend; a review
# pause inside the Job is not a completion.
# ---------------------------------------------------------------------------

TASK = "p:042-x"
SPEC = "042-x"
PHASED = {
    "phases": [{"name": "b", "subtasks": [{"id": "1", "status": "completed"}]}],
    "status": "in_progress",
}
USAGE = {
    "totalInputTokens": 400,
    "outputTokens": 100,
    "totalTokens": 500,
    "totalCostUsd": 0.01,
    "model": "m",
}


class _KjStore:
    def __init__(self) -> None:
        self.terminal: list[tuple[Any, ...]] = []

    async def get_state(self, _job_id: str) -> dict[str, Any]:
        return {"lifecycle_state": "running", "worker_ref": {"kind": "k8s-job"}}

    async def mark_terminal(self, *a: Any, **_kw: Any) -> None:
        self.terminal.append(a)


class _Backend:
    async def delete_job(self, _task_id: str) -> None:
        return None


def _kj(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    plan: dict[str, Any] | None = None,
    remote_plan: dict[str, Any] | None = None,
    usage: bool = True,
) -> SimpleNamespace:
    """A control-plane AgentService with no DB: fake object store, tmp project."""
    store = a_s._fake_store()
    monkeypatch.setattr(a_s, "ArtifactStore", lambda *a, **k: store)
    if usage:
        store.put_bytes(wf._usage_key(SPEC), json.dumps(USAGE).encode())
    if remote_plan is not None:
        store.put_bytes(wf._plan_key(SPEC), json.dumps(remote_plan).encode())
    spec_dir = tmp_path / ".aifactory" / "specs" / SPEC
    spec_dir.mkdir(parents=True)
    if plan is not None:
        (spec_dir / "implementation_plan.json").write_text(json.dumps(plan))
    # Both the module-level import and the lazy one in _emit_kubejob_terminal_completion.
    monkeypatch.setattr(agent_kubejob, "resolve_project_path", lambda _p: tmp_path)
    monkeypatch.setattr(
        "server.project_registry.resolve_project_path", lambda _p: tmp_path
    )
    events: list[dict[str, Any]] = []
    monkeypatch.setattr(
        completion, "notify_completion", lambda e, **k: events.append(e) or True
    )
    svc = AgentService.__new__(AgentService)
    svc.settings = SimpleNamespace(BACKEND_PATH=str(tmp_path / "backend"))  # type: ignore[assignment]
    svc._kubejob_log_streamers = {}
    kstore = _KjStore()
    orphans: list[str] = []
    drained: list[int] = []

    async def drain() -> None:
        drained.append(1)

    async def noop(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(svc, "_store", lambda: kstore)
    monkeypatch.setattr(svc, "_build_backend", lambda: _Backend())
    monkeypatch.setattr(svc, "_drain_queue", drain)
    monkeypatch.setattr(svc, "_release_task_credential", lambda _t: None)
    monkeypatch.setattr(svc, "_safe_emit_task_status", noop)
    monkeypatch.setattr(svc, "_safe_emit_task_update", noop)
    monkeypatch.setattr(svc, "_reap_kubejob_console", noop)
    monkeypatch.setattr(
        agent_kubejob, "_report_orphaned_worktrees", lambda j: orphans.append(j) or []
    )
    return SimpleNamespace(
        svc=svc,
        spec_dir=spec_dir,
        events=events,
        store=kstore,
        orphans=orphans,
        drained=drained,
    )


def _statuses(k: SimpleNamespace) -> list[str]:
    return [e["status"] for e in k.events]


async def test_kubejob_failed_without_phases_sends_one_failed_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    k = _kj(tmp_path, monkeypatch)
    await k.svc._on_kubejob_build_failed(TASK, "boom")
    assert _statuses(k) == ["failed"]
    assert k.events[0]["usage"]["total_tokens"] == 500
    assert k.drained == [1]


async def test_kubejob_failed_with_phases_sends_one_failed_event_total(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The terminal event already carried the usage; the helper must see no NEW
    # data and stay silent (it runs after _record_kubejob_terminal).
    k = _kj(tmp_path, monkeypatch, plan=PHASED)
    await k.svc._on_kubejob_build_failed(TASK, "boom")
    assert _statuses(k) == ["failed"]
    assert k.events[0]["usage"]["total_tokens"] == 500


@pytest.mark.parametrize("usage", [True, False], ids=["object", "no_object"])
async def test_kubejob_stop_reports_usage_and_leaves_control(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, usage: bool
) -> None:
    k = _kj(tmp_path, monkeypatch, usage=usage)
    task_control.write_control(k.spec_dir, status="in_progress", updated_by="before")
    assert await k.svc._stop_kubejob_build(TASK) is True
    assert _statuses(k) == (["failed"] if usage else [])
    ctrl = task_control.read_control(k.spec_dir)
    assert (ctrl["status"], ctrl["updatedBy"]) == ("in_progress", "before")
    assert len(k.store.terminal) == 1
    assert k.drained == [1]


async def test_kubejob_reap_reports_one_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    k = _kj(tmp_path, monkeypatch)
    task = SimpleNamespace(
        id=TASK, status="in_progress", updated_at="2000-01-01T00:00:00+00:00"
    )
    monkeypatch.setattr(
        "server.project_registry.load_projects",
        lambda: {"p": {"path": str(tmp_path)}},
    )
    monkeypatch.setattr(
        "server.routes.task_service.get_spec_dirs", lambda _p: [k.spec_dir]
    )
    monkeypatch.setattr("server.routes.task_service.spec_to_task", lambda _p, _s: task)
    monkeypatch.setattr(k.svc, "is_running", lambda _t: False)

    async def absent(_t: str) -> str:
        return "absent"

    monkeypatch.setattr(k.svc, "_kubejob_liveness", absent)
    assert await k.svc.reap_abandoned_tasks() == [TASK]
    assert _statuses(k) == ["failed"]


@pytest.mark.parametrize("path", ["failed", "stop"])
async def test_kubejob_usage_helper_raising_never_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    k = _kj(tmp_path, monkeypatch)

    def boom(*_a: Any, **_k: Any) -> bool:
        raise RuntimeError("store down")

    monkeypatch.setattr(wf, "maybe_fetch_usage", boom)
    if path == "failed":
        await k.svc._on_kubejob_build_failed(TASK, "boom")
    else:
        assert await k.svc._stop_kubejob_build(TASK) is True
        assert len(k.store.terminal) == 1
    assert k.drained == [1]


# ── C4: a review pause in the Job is not a completion ──


def _rtc(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    rtc = AsyncMock(return_value="completed")
    monkeypatch.setattr(completion_orchestration, "run_terminal_completion", rtc)
    return rtc


def _assert_paused(k: SimpleNamespace, rtc: AsyncMock, reason: str) -> None:
    ctrl = task_control.read_control(k.spec_dir)
    assert ctrl["status"] == "human_review"
    assert ctrl["reviewReason"] == reason
    assert ctrl["updatedBy"] == "kubejob_review_pause"
    assert _statuses(k) == ["human_review"]
    assert rtc.await_count == 0
    assert not (k.spec_dir / ".terminal_completion_emitted").exists()
    plan = json.loads((k.spec_dir / "implementation_plan.json").read_text())
    assert plan["status"] == "human_review"
    assert k.orphans == [TASK]
    assert k.drained == [1]


@pytest.mark.parametrize("reason", ["plan_review", "injection_scan"])
async def test_kubejob_review_pause_is_not_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    k = _kj(
        tmp_path,
        monkeypatch,
        plan=PHASED,
        remote_plan={**PHASED, "status": "human_review", "reviewReason": reason},
    )
    rtc = _rtc(monkeypatch)
    await k.svc._on_kubejob_build_done(TASK)
    _assert_paused(k, rtc, reason)


async def test_kubejob_preflight_skeleton_plan_pauses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    k = _kj(
        tmp_path,
        monkeypatch,
        remote_plan={
            "phases": [],
            "status": "human_review",
            "reviewReason": "plan_review",
        },
    )
    rtc = _rtc(monkeypatch)
    await k.svc._on_kubejob_build_done(TASK)
    _assert_paused(k, rtc, "plan_review")


@pytest.mark.parametrize(
    "remote",
    [
        {**PHASED, "status": "human_review", "reviewReason": "completed"},
        {**PHASED, "status": "human_review", "reviewReason": "qa_issues"},
        PHASED,
    ],
    ids=["hr_completed", "hr_qa_issues", "in_progress"],
)
async def test_kubejob_other_statuses_keep_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, remote: dict[str, Any]
) -> None:
    k = _kj(tmp_path, monkeypatch, plan=PHASED, remote_plan=remote)
    rtc = _rtc(monkeypatch)
    await k.svc._on_kubejob_build_done(TASK)
    assert rtc.await_count >= 1
    assert any(c.kwargs.get("is_completed") is True for c in rtc.await_args_list)
    assert (
        task_control.read_control(k.spec_dir).get("updatedBy") != "kubejob_review_pause"
    )
    assert k.events == []
    assert k.drained == [1]
