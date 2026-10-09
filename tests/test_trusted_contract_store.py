"""trusted_contracts table at Alembic head (test 7) and the sticky stamp (test 13) (#1667)."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.postgres.helpers import WEB_SERVER_ROOT, run_alembic

if str(WEB_SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(WEB_SERVER_ROOT))

COLUMNS = {
    "spec_key",
    "spec_id",
    "contract",
    "build_isolation",
    "created_at",
    "updated_at",
}


def test_7_table_exists_at_head_sqlite(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    result = run_alembic(
        ["upgrade", "head"], env={"DATABASE_URL": f"sqlite+aiosqlite:///{db}"}
    )
    assert result.returncode == 0, result.stderr[-1000:]

    with sqlite3.connect(db) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(trusted_contracts)")}
    assert cols == COLUMNS


async def test_13_stamp_is_sticky_and_missing_row_is_noop(tmp_path: Path) -> None:
    from server.database.models import Base
    from server.services.trusted_contract_store import TrustedContractStore

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 's.db'}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        store = TrustedContractStore(
            session_factory=async_sessionmaker(engine, expire_on_commit=False)
        )

        await store.stamp_isolation("missing", "kubejob")  # no row: no-op, no raise
        assert await store.get("missing") is None

        await store.put("k", "001-x", {"feature": "f"})
        await store.stamp_isolation("k", "none")
        await store.stamp_isolation("k", "kubejob")

        rec = await store.get("k")
        assert rec is not None
        assert rec.build_isolation == "none"
        assert rec.contract == {"feature": "f"}
    finally:
        await engine.dispose()


async def test_13b_put_resets_the_stamp_only_when_the_contract_changes(
    tmp_path: Path,
) -> None:
    from server.database.models import Base
    from server.services.trusted_contract_store import TrustedContractStore

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 's.db'}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        store = TrustedContractStore(
            session_factory=async_sessionmaker(engine, expire_on_commit=False)
        )
        await store.put("k", "001-x", {"feature": "f"})
        await store.stamp_isolation("k", "none")

        await store.put("k", "001-x", {"feature": "f"})  # same contract: kept
        rec = await store.get("k")
        assert rec is not None and rec.build_isolation == "none"

        await store.put("k", "001-x", {"feature": "g"})  # new contract: new build
        rec = await store.get("k")
        assert rec is not None and rec.build_isolation is None
    finally:
        await engine.dispose()


class _FailingStore:
    def __init__(self, *_a: object, **_k: object) -> None:
        pass

    async def stamp_isolation(self, *_a: object) -> None:
        raise RuntimeError("db down")


async def test_13c_failed_none_stamp_blocks_the_spawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An earlier isolated stamp must not survive a non-isolated build."""
    from server.services import trusted_contract_store as tcs

    monkeypatch.setattr(tcs, "TrustedContractStore", _FailingStore)
    with pytest.raises(RuntimeError):
        await tcs.stamp_spawn("/x/.aifactory/specs/001", "none")
    await tcs.stamp_spawn("/x/.aifactory/specs/001", "kubejob")  # logged, no raise


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
