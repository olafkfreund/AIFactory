"""#1551: the reaper must fail toward LEAVING WORK ALONE, not toward reaping
-- for the backend actually in play.

``_kubejob_liveness`` replaces the old ``_has_live_kubejob`` bool: a transient
read error or a row in a state that isn't a proven terminal used to collapse
to ``False`` -> "no live build" -> reaped. These tests exercise the real
method against a real (sqlite-backed) JobStateStore, not a stand-in, so a
regression that turns "unknown" back into "absent" is caught here.

A SECOND review finding (same issue): treating "no durable store" as
"unknown" leaked abandoned in-pod subprocess tasks forever, because with no
store the kubejob backend never dispatches a Job (see
``_kubejob_backend_enabled``) -- every build is a single-pod subprocess, and
``is_running()``, already checked by the caller, is the complete answer. So
"no store" must read as a definite "absent", not "unknown".
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

_ROOT = Path(__file__).resolve().parents[1] / "apps" / "web-server"
if str(_ROOT) not in sys.path:
    sys.path.append(str(_ROOT))

from server.database.models import Base  # noqa: E402
from server.services.agent_kubejob import KubejobMixin  # noqa: E402
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


class _Owner(KubejobMixin):
    """Just enough of AgentService for `_kubejob_liveness` to run standalone."""

    def __init__(self, store: JobStateStore | None, *, store_enabled: bool):
        self._job_store = store
        self._store_enabled = store_enabled

    def _store(self):
        return self._job_store


async def test_running_row_is_live(tmp_path: Path):
    store = await _make_store(tmp_path / "a.db")
    await store.admit("t1", SpawnArgs(project_path="/p", spec_id="s1"), cap=0)
    owner = _Owner(store, store_enabled=True)
    assert await owner._kubejob_liveness("t1") == "live"


async def test_terminal_row_is_absent(tmp_path: Path):
    store = await _make_store(tmp_path / "b.db")
    await store.admit("t1", SpawnArgs(project_path="/p", spec_id="s1"), cap=0)
    await store.mark_terminal("t1", "failed", error="boom")
    owner = _Owner(store, store_enabled=True)
    assert await owner._kubejob_liveness("t1") == "absent"


async def test_no_row_is_unknown_not_absent(tmp_path: Path):
    """A row should always exist for an admitted task; a miss is the anomaly
    #1551 tracks, not a clean "never built" answer — never reap on it."""
    store = await _make_store(tmp_path / "c.db")
    owner = _Owner(store, store_enabled=True)
    assert await owner._kubejob_liveness("never-admitted") == "unknown"


async def test_store_disabled_is_absent_not_unknown(tmp_path: Path):
    """Mutation guard: with no durable store, every build in this deployment
    is an in-pod subprocess (the kubejob backend refuses to dispatch without
    the store) -- is_running(), already checked by the caller, is the full
    answer, so this must read as a definite "absent", not "unknown". If this
    regresses to "unknown", a dead subprocess task leaks in_progress forever
    and the concurrency cap shrinks on every occurrence."""
    owner = _Owner(None, store_enabled=False)
    assert await owner._kubejob_liveness("t1") == "absent"


async def test_store_read_error_is_unknown_not_absent(tmp_path: Path):
    """Mutation guard: if the exception path ever goes back to reading as
    "absent" (the pre-#1551 defect: `except Exception: return False`), this
    must fail."""

    class _BrokenStore:
        async def get_state(self, task_id: str):
            raise RuntimeError("connection pool exhausted")

    owner = _Owner(_BrokenStore(), store_enabled=True)
    assert await owner._kubejob_liveness("t1") == "unknown"


async def test_queued_row_is_unknown_not_absent(tmp_path: Path):
    """A row can be non-terminal without being "running" (e.g. "queued" —
    ``reap_abandoned_tasks`` should never see this for a frontend-in_progress
    task, but the predicate must still refuse to treat it as a green light to
    reap)."""
    store = await _make_store(tmp_path / "d.db")
    await store.admit("t1", SpawnArgs(project_path="/p", spec_id="s1"), cap=1)
    await store.admit(
        "t2", SpawnArgs(project_path="/p", spec_id="s1"), cap=1
    )  # queued, cap=1
    owner = _Owner(store, store_enabled=True)
    assert await owner._kubejob_liveness("t2") == "unknown"


# ── reap_abandoned_tasks end-to-end, real _kubejob_liveness (no stand-in) ──


async def test_store_disabled_reaps_dead_subprocess(monkeypatch, tmp_path: Path):
    """The leak this fix closes: AIFACTORY_BUILD_BACKEND=kubejob configured
    WITHOUT DATABASE_URL still starts the reconcile loop (main.py gates it on
    kubejob_enabled() alone), so reap_abandoned_tasks runs with
    ``_store_enabled=False``. A subprocess that died without updating its
    status must still be reaped -- not leaked forever with the cap shrinking
    each time."""
    from datetime import UTC, datetime, timedelta

    class _Reaper(KubejobMixin):
        def __init__(self):
            self._store_enabled = False
            self._job_store = None
            self.reaped_calls: list[tuple[str, str]] = []

        def is_running(self, task_id: str) -> bool:
            return False  # the subprocess died; nothing left in this pod

        async def _update_plan_status(
            self, project_path, spec_id, status, task_id, **kw
        ):
            self.reaped_calls.append((task_id, status))

    stale_iso = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    task = SimpleNamespace(id="p:x", status="in_progress", updated_at=stale_iso)
    monkeypatch.setattr(
        "server.project_registry.load_projects", lambda: {"p": {"path": "/x"}}
    )
    spec_dir = Path("/x/specs/x")
    monkeypatch.setattr(
        "server.routes.task_service.get_spec_dirs", lambda pp: [spec_dir]
    )
    monkeypatch.setattr("server.routes.task_service.spec_to_task", lambda pid, sd: task)

    r = _Reaper()
    assert await r.reap_abandoned_tasks(deadline_seconds=600) == ["p:x"]
    assert r.reaped_calls == [("p:x", "failed")]
