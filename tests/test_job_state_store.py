"""Durable job-state store tests (RFC-0016 #668) — fast lane.

These exercise ``services/job_state_store.JobStateStore`` against a real
SQLite (aiosqlite) file DB so they run in the default ``backend (ruff +
pytest)`` gate without a Postgres server. They cover the durability
contract from ``apis/concurrency-conventions.md``:

* round-trip a job-state row (schema fields persist);
* the admission cap reads live counts from the store (not memory);
* FIFO dequeue on drain when a slot frees;
* restart-recovery: a NEW store instance reconstructs in-flight
  queued/running rows from the same table;
* the in-memory fallback engages when ``DATABASE_URL`` is unset.

Cross-replica ``SELECT ... FOR UPDATE`` safety (which SQLite cannot model)
is verified against real Postgres in
``tests/postgres/test_job_states_concurrency.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

PROJECT_ROOT = Path(__file__).resolve().parents[1] / "apps" / "web-server"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.append(str(PROJECT_ROOT))

from server.database.models import Base, JobState  # noqa: E402
from server.services.job_state_store import (  # noqa: E402
    JobStateStore,
    SpawnArgs,
    store_enabled,
)

_ENGINES: list = []


async def _make_factory(db_path: Path) -> async_sessionmaker:
    """A session factory bound to a fresh SQLite file with job_states created.

    Engines are tracked so the ``_dispose_engines`` autouse fixture can close
    their background aiosqlite threads at test teardown (avoids
    'Event loop is closed' resource warnings under asyncio_mode=auto).
    """
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


def _spawn(spec_id: str, path: Path) -> SpawnArgs:
    return SpawnArgs(project_path=str(path), spec_id=spec_id, parallel=True, workers=3)


# --------------------------------------------------------------------------
# fallback gating
# --------------------------------------------------------------------------


def test_store_disabled_when_database_url_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert store_enabled() is False


def test_store_disabled_when_database_url_blank(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "   ")
    assert store_enabled() is False


def test_store_enabled_when_database_url_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@db/x")
    assert store_enabled() is True


# --------------------------------------------------------------------------
# round-trip
# --------------------------------------------------------------------------


async def test_round_trip_row(tmp_path: Path) -> None:
    factory = await _make_factory(tmp_path / "rt.db")
    store = JobStateStore(session_factory=factory)

    outcome = await store.admit(
        "proj:spec-1", _spawn("spec-1", tmp_path), cap=5, correlation_key="612"
    )
    assert outcome == "started"

    async with factory() as session:
        row = await session.get(JobState, "proj:spec-1")
        assert row is not None
        assert row.schema_version == "1"
        assert row.service == "aifactory"
        assert row.kind == "build"
        assert row.lifecycle_state == "running"
        assert row.correlation_key == "612"
        assert row.attempt == 1
        assert row.worker_ref == {"kind": "subprocess"}
        assert row.spawn_args["spec_id"] == "spec-1"
        assert row.spawn_args["parallel"] is True
        assert row.spawn_args["workers"] == 3
        assert row.admission["started_at"] is not None


async def test_terminal_sets_result_error_and_frees_slot(tmp_path: Path) -> None:
    factory = await _make_factory(tmp_path / "term.db")
    store = JobStateStore(session_factory=factory)

    await store.admit("p:s1", _spawn("s1", tmp_path), cap=5)
    await store.mark_terminal(
        "p:s1", "done", result={"pr_url": "http://x/1"}, usage={"total_tokens": 9}
    )

    async with factory() as session:
        row = await session.get(JobState, "p:s1")
        assert row.lifecycle_state == "done"
        assert row.result == {"pr_url": "http://x/1"}
        assert row.usage == {"total_tokens": 9}
        assert row.ended_at is not None
    # Slot freed: running count is 0 now.
    assert await store.get_running_job_ids() == []


async def test_failed_terminal_requires_error_never_overclaim(tmp_path: Path) -> None:
    factory = await _make_factory(tmp_path / "fail.db")
    store = JobStateStore(session_factory=factory)
    await store.admit("p:s1", _spawn("s1", tmp_path), cap=5)
    # No explicit error supplied — store must synthesise one (never-overclaim).
    await store.mark_terminal("p:s1", "failed")
    async with factory() as session:
        row = await session.get(JobState, "p:s1")
        assert row.lifecycle_state == "failed"
        assert row.error  # non-empty


# --------------------------------------------------------------------------
# cap / queue counts come from the store
# --------------------------------------------------------------------------


async def test_cap_counts_come_from_store(tmp_path: Path) -> None:
    factory = await _make_factory(tmp_path / "cap.db")
    store = JobStateStore(session_factory=factory)

    assert await store.admit("p:a", _spawn("a", tmp_path), cap=2) == "started"
    assert await store.admit("p:b", _spawn("b", tmp_path), cap=2) == "started"
    # At cap -> queued, not started.
    assert await store.admit("p:c", _spawn("c", tmp_path), cap=2) == "queued"

    assert sorted(await store.get_running_job_ids()) == ["p:a", "p:b"]
    assert await store.get_queued_job_ids() == ["p:c"]
    assert await store.is_running("p:a")
    assert await store.is_queued("p:c")


async def test_cap_zero_is_unlimited(tmp_path: Path) -> None:
    factory = await _make_factory(tmp_path / "unl.db")
    store = JobStateStore(session_factory=factory)
    for i in range(8):
        assert await store.admit(f"p:{i}", _spawn(str(i), tmp_path), cap=0) == "started"
    assert await store.get_queued_job_ids() == []
    assert len(await store.get_running_job_ids()) == 8


async def test_duplicate_active_job_id_raises(tmp_path: Path) -> None:
    factory = await _make_factory(tmp_path / "dup.db")
    store = JobStateStore(session_factory=factory)
    await store.admit("p:a", _spawn("a", tmp_path), cap=5)
    with pytest.raises(ValueError, match="already running"):
        await store.admit("p:a", _spawn("a", tmp_path), cap=5)


# --------------------------------------------------------------------------
# FIFO dequeue on drain
# --------------------------------------------------------------------------


async def test_fifo_drain_on_finished_build(tmp_path: Path) -> None:
    factory = await _make_factory(tmp_path / "fifo.db")
    store = JobStateStore(session_factory=factory)

    await store.admit("p:a", _spawn("a", tmp_path), cap=1)  # running
    await store.admit("p:b", _spawn("b", tmp_path), cap=1)  # queued
    await store.admit("p:c", _spawn("c", tmp_path), cap=1)  # queued
    assert await store.get_queued_job_ids() == ["p:b", "p:c"]

    # Build a finishes -> one free slot -> next FIFO promoted.
    await store.mark_terminal("p:a", "done")
    promoted = await store.drain(cap=1)
    assert [jid for jid, _ in promoted] == ["p:b"]
    assert await store.is_running("p:b")
    assert await store.get_queued_job_ids() == ["p:c"]
    # Promoted spawn args round-trip.
    assert promoted[0][1].spec_id == "b"

    await store.mark_terminal("p:b", "done")
    promoted2 = await store.drain(cap=1)
    assert [jid for jid, _ in promoted2] == ["p:c"]
    assert await store.get_queued_job_ids() == []


async def test_drain_noop_when_no_free_slot(tmp_path: Path) -> None:
    factory = await _make_factory(tmp_path / "noslot.db")
    store = JobStateStore(session_factory=factory)
    await store.admit("p:a", _spawn("a", tmp_path), cap=1)  # running
    await store.admit("p:b", _spawn("b", tmp_path), cap=1)  # queued
    # No slot free (a still running) -> nothing promoted.
    assert await store.drain(cap=1) == []
    assert await store.get_queued_job_ids() == ["p:b"]


async def test_stop_removes_queued(tmp_path: Path) -> None:
    factory = await _make_factory(tmp_path / "stop.db")
    store = JobStateStore(session_factory=factory)
    await store.admit("p:a", _spawn("a", tmp_path), cap=1)  # running
    await store.admit("p:b", _spawn("b", tmp_path), cap=1)  # queued
    assert await store.remove_queued("p:b") is True
    assert await store.get_queued_job_ids() == []
    # A running task is not removable via remove_queued.
    assert await store.remove_queued("p:a") is False


# --------------------------------------------------------------------------
# restart-recovery
# --------------------------------------------------------------------------


async def test_restart_recovery_reconstructs_inflight(tmp_path: Path) -> None:
    db = tmp_path / "restart.db"
    factory = await _make_factory(db)

    # Replica 1 admits some work, then "dies".
    store1 = JobStateStore(session_factory=factory)
    await store1.admit("p:a", _spawn("a", tmp_path), cap=2)  # running
    await store1.admit("p:b", _spawn("b", tmp_path), cap=2)  # running
    await store1.admit("p:c", _spawn("c", tmp_path), cap=2)  # queued

    # Replica 2 boots with a brand-new store instance + a fresh session
    # factory over the SAME file -> reconstructs from Postgres/SQLite, not
    # from an empty in-memory view.
    engine2 = create_async_engine(f"sqlite+aiosqlite:///{db}")
    _ENGINES.append(engine2)
    factory2 = async_sessionmaker(engine2, expire_on_commit=False)
    store2 = JobStateStore(session_factory=factory2)
    state = await store2.reconstruct()

    assert sorted(jid for jid, _ in state["running"]) == ["p:a", "p:b"]
    assert [jid for jid, _ in state["queued"]] == ["p:c"]
    # Reconstructed spawn args survived the "restart".
    running_specs = {jid: sa.spec_id for jid, sa in state["running"]}
    assert running_specs["p:a"] == "a"


async def test_increment_attempt(tmp_path: Path) -> None:
    factory = await _make_factory(tmp_path / "attempt.db")
    store = JobStateStore(session_factory=factory)
    await store.admit("p:a", _spawn("a", tmp_path), cap=5)
    await store.increment_attempt("p:a")
    await store.increment_attempt("p:a")
    async with factory() as session:
        row = await session.get(JobState, "p:a")
        assert row.attempt == 3
