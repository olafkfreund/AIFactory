"""investigations schema + store on Postgres (#2156, step 1)."""

from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.postgres.helpers import alembic_available, run_alembic, sync_url

WEB_SERVER_ROOT = Path(__file__).resolve().parents[2] / "apps" / "web-server"
if str(WEB_SERVER_ROOT) not in sys.path:
    sys.path.append(str(WEB_SERVER_ROOT))

from server.services.investigation_store import (  # noqa: E402
    InvestigationStore,
    SlotTaken,
    _now,
)

pytestmark = [pytest.mark.postgres, pytest.mark.slow]


@pytest.fixture
async def store(test_postgres_url: str):
    if not alembic_available():
        pytest.skip("alembic CLI not on PATH")
    res = run_alembic(["upgrade", "head"], env={"DATABASE_URL": test_postgres_url})
    assert res.returncode == 0, res.stderr[-1000:]
    engine = create_async_engine(test_postgres_url)
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM investigation_logs"))
        await conn.execute(text("DELETE FROM investigations"))
    s = InvestigationStore(async_sessionmaker(engine, expire_on_commit=False))
    yield s
    await engine.dispose()


def _run(project: str = "p1", **kw):
    return {
        "id": str(uuid.uuid4()),
        "project_id": project,
        "repo": "o/r",
        "sha": "a" * 40,
        "request": "why?",
        "limits": {"minutes": 30},
        "state": "queued",
        "lease_expires_at": _now() + timedelta(seconds=60),
        "token_hash": "0" * 64,
        **kw,
    }


async def test_concurrent_inserts_one_slot_taken(store) -> None:
    results = await asyncio.gather(
        store.insert(**_run()), store.insert(**_run()), return_exceptions=True
    )
    assert sum(isinstance(r, SlotTaken) for r in results) == 1
    assert len(await store.live_runs()) == 1


async def test_uncertain_still_blocks_and_pods_gone_frees(store) -> None:
    run = await store.insert(**_run())
    await store.update(run.id, state="uncertain")
    with pytest.raises(SlotTaken):
        await store.insert(**_run())
    await store.update(run.id, pods_gone=True)
    await store.insert(**_run())  # slot free again
    await store.insert(**_run(project="other"))  # other projects unaffected


async def test_lock_updates_in_transaction(store) -> None:
    run = await store.insert(**_run())
    async with store.lock(run.id) as (session, row):
        await store.update(row.id, session=session, state="running")
    assert (await store.get(run.id)).state == "running"

    async def fail_inside_lock() -> None:
        async with store.lock(run.id) as (session, row):
            await store.update(row.id, session=session, state="failed")
            raise RuntimeError

    with pytest.raises(RuntimeError):
        await fail_inside_lock()
    assert (await store.get(run.id)).state == "running"


async def test_log_cursor_advances_and_limit_capped(store) -> None:
    run = await store.insert(**_run())
    await store.append_logs(run.id, [f"l{i}" for i in range(600)])
    first = await store.read_logs(run.id, 0, 2)
    assert [e["line"] for e in first["entries"]] == ["l0", "l1"]
    second = await store.read_logs(run.id, first["cursor"], 2)
    assert [e["line"] for e in second["entries"]] == ["l2", "l3"]
    third = await store.read_logs(run.id, second["cursor"], 10_000)
    assert len(third["entries"]) == 500  # capped
    idle = await store.read_logs(run.id, 10**9)
    assert idle == {"entries": [], "cursor": 10**9}


async def test_purge_only_runs_ended_over_cutoff(store) -> None:
    old = await store.insert(**_run(project="a", pods_gone=True))
    new = await store.insert(**_run(project="b", pods_gone=True))
    live = await store.insert(**_run(project="c"))
    now = _now()
    await store.update(old.id, ended_at=now - timedelta(days=8))
    await store.update(new.id, ended_at=now - timedelta(days=1))
    for r in (old, new, live):
        await store.append_logs(r.id, ["x"])
    assert await store.purge_logs(now - timedelta(days=7)) == 1
    assert (await store.read_logs(old.id))["entries"] == []
    assert len((await store.read_logs(new.id))["entries"]) == 1
    assert len((await store.read_logs(live.id))["entries"]) == 1


def test_downgrade_drops_both_tables(test_postgres_url: str) -> None:
    env = {"DATABASE_URL": test_postgres_url}
    assert run_alembic(["upgrade", "head"], env=env).returncode == 0
    down = run_alembic(["downgrade", "7a1f3c9e2b50"], env=env)
    assert down.returncode == 0, down.stderr[-1000:]
    engine = create_engine(sync_url(test_postgres_url))
    with engine.connect() as conn:
        left = conn.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_name IN ('investigations','investigation_logs')"
            )
        ).all()
    engine.dispose()
    assert left == []
    assert run_alembic(["upgrade", "head"], env=env).returncode == 0
