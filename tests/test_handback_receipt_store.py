"""handback_receipts at Alembic head, and the store fails closed (#1696)."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

from tests.postgres.helpers import WEB_SERVER_ROOT, run_alembic

if str(WEB_SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(WEB_SERVER_ROOT))

from server.services import handback_receipt_store as hrs  # noqa: E402

# Captured before the web-server conftest stub can replace it.
_REAL_ON_RECORD = hrs.on_record


def test_table_exists_at_head_sqlite(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    result = run_alembic(
        ["upgrade", "head"], env={"DATABASE_URL": f"sqlite+aiosqlite:///{db}"}
    )
    assert result.returncode == 0, result.stderr[-1000:]
    with sqlite3.connect(db) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(handback_receipts)")}
    assert cols == {"spec_key", "spec_id", "received_at"}


async def test_a_failed_read_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom() -> None:
        raise RuntimeError("db down")

    monkeypatch.setattr(hrs, "on_record", _REAL_ON_RECORD)
    monkeypatch.setattr("server.services.job_state_store.store_enabled", lambda: True)
    monkeypatch.setattr("server.database.engine.async_session_factory", boom)
    assert await hrs.on_record(tmp_path) is True
    await hrs.record(tmp_path)  # logged, never raised


async def test_no_store_means_no_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hrs, "on_record", _REAL_ON_RECORD)
    monkeypatch.setattr("server.services.job_state_store.store_enabled", lambda: False)
    assert await hrs.on_record(tmp_path) is False
