"""Cross-replica admission-safety tests for the durable store (RFC-0016 #668).

The whole point of moving the cap/queue into Postgres is that two
control-plane replicas reading the SAME database cannot exceed
``MAX_CONCURRENT_TASKS`` or double-start the same ``job_id``. SQLite cannot
model that (no real ``SELECT ... FOR UPDATE`` / advisory locks), so these run
against real Postgres only.

Each "replica" is a separate async engine + ``JobStateStore``. We fire many
admits concurrently and assert the durable invariants hold.

Marked ``postgres``/``slow`` — runs in the ``postgres-acceptance`` CI job.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.postgres.helpers import alembic_available, run_alembic

WEB_SERVER_ROOT = Path(__file__).resolve().parents[2] / "apps" / "web-server"
if str(WEB_SERVER_ROOT) not in sys.path:
    sys.path.append(str(WEB_SERVER_ROOT))

from server.services.job_state_store import (  # noqa: E402
    SERVICE,
    JobStateStore,
    SpawnArgs,
)


def _replica(url: str) -> JobStateStore:
    """A JobStateStore on its own engine — simulates one control-plane pod."""
    engine = create_async_engine(url, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    return JobStateStore(session_factory=factory)


async def _ensure_schema(url: str) -> None:
    if not alembic_available():
        pytest.skip("alembic CLI not on PATH")
    res = run_alembic(["upgrade", "head"], env={"DATABASE_URL": url})
    assert res.returncode == 0, f"upgrade failed: {res.stderr[-1000:]}"


async def _wipe(url: str) -> None:
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM job_states WHERE service = :s"), {"s": SERVICE}
        )
    await engine.dispose()


def _spawn(spec: str) -> SpawnArgs:
    return SpawnArgs(project_path="/tmp/x", spec_id=spec)


@pytest.mark.postgres
@pytest.mark.slow
async def test_for_update_never_exceeds_cap_across_replicas(
    test_postgres_url: str,
) -> None:
    await _ensure_schema(test_postgres_url)
    await _wipe(test_postgres_url)

    cap = 3
    n = 20
    # Two replicas, each with its own engine/pool against the same DB.
    r1 = _replica(test_postgres_url)
    r2 = _replica(test_postgres_url)
    replicas = [r1, r2]

    run_id = uuid.uuid4().hex[:8]

    async def admit(i: int) -> str:
        store = replicas[i % 2]
        return await store.admit(f"p:{run_id}:{i:03d}", _spawn(str(i)), cap=cap)

    outcomes = await asyncio.gather(*[admit(i) for i in range(n)])

    started = outcomes.count("started")
    queued = outcomes.count("queued")
    assert started == cap, f"cap exceeded/under: started={started}, expected {cap}"
    assert queued == n - cap
    # The durable running count must match exactly — no over-admission.
    assert len(await r1.get_running_job_ids()) == cap

    await _wipe(test_postgres_url)


@pytest.mark.postgres
@pytest.mark.slow
async def test_for_update_no_double_start_same_job_id(
    test_postgres_url: str,
) -> None:
    await _ensure_schema(test_postgres_url)
    await _wipe(test_postgres_url)

    r1 = _replica(test_postgres_url)
    r2 = _replica(test_postgres_url)
    run_id = uuid.uuid4().hex[:8]
    job_id = f"p:{run_id}:dup"

    async def admit(store: JobStateStore) -> object:
        try:
            return await store.admit(job_id, _spawn("dup"), cap=5)
        except ValueError as e:
            return e

    res = await asyncio.gather(admit(r1), admit(r2))
    started = [r for r in res if r == "started"]
    errors = [r for r in res if isinstance(r, ValueError)]
    # Exactly one admit wins; the other sees "already running" (409).
    assert len(started) == 1, f"double-start: {res}"
    assert len(errors) == 1, f"expected one 409, got {res}"
    assert len(await r1.get_running_job_ids()) == 1

    await _wipe(test_postgres_url)


@pytest.mark.postgres
@pytest.mark.slow
async def test_drain_promotes_without_exceeding_cap_across_replicas(
    test_postgres_url: str,
) -> None:
    await _ensure_schema(test_postgres_url)
    await _wipe(test_postgres_url)

    cap = 2
    r1 = _replica(test_postgres_url)
    r2 = _replica(test_postgres_url)
    run_id = uuid.uuid4().hex[:8]

    ids = [f"p:{run_id}:{i}" for i in range(5)]
    for i, jid in enumerate(ids):
        await r1.admit(jid, _spawn(str(i)), cap=cap)
    assert len(await r1.get_running_job_ids()) == cap
    assert len(await r1.get_queued_job_ids()) == 3

    # Free both running slots, then both replicas drain concurrently.
    running = await r1.get_running_job_ids()
    for jid in running:
        await r1.mark_terminal(jid, "done")

    promoted = await asyncio.gather(r1.drain(cap), r2.drain(cap))
    total_promoted = sum(len(p) for p in promoted)
    # Exactly the freed slots get filled — never more than the cap is running.
    assert total_promoted == cap
    assert len(await r1.get_running_job_ids()) == cap

    await _wipe(test_postgres_url)
