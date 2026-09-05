"""audit_logs.resource_id must hold a composite task id (#1458).

``resource_id`` was ``String(36)`` -- a UUID's length -- while the task pipeline
builds ``"{project_id}:{spec-slug}"``, 53+ characters. Postgres rejected the
insert with StringDataRightTruncationError and
``audit_service.log_audit_event_bg`` swallowed it at WARNING, so the API
returned success and no row was ever written.

These tests run against POSTGRES on purpose. SQLite does not enforce VARCHAR
lengths at all, so the same test on the in-memory SQLite the rest of the audit
suite uses would pass with the column at 36 and prove nothing.

The mutation check is ``test_composite_resource_id_actually_lands``: it drives
the REAL writer and asserts the ROW EXISTS. Asserting only that no exception
escaped passes on the broken schema -- the exception being swallowed is the
defect.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, delete, select, text

from tests.postgres.helpers import (
    WEB_SERVER_ROOT,
    alembic_available,
    run_alembic,
)

if str(WEB_SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(WEB_SERVER_ROOT))


def _sync_url(url: str) -> str:
    return url.replace("+asyncpg", "")


def _upgrade_head(test_postgres_url: str) -> None:
    result = run_alembic(["upgrade", "head"], env={"DATABASE_URL": test_postgres_url})
    assert result.returncode == 0, f"upgrade failed: {result.stderr[-1000:]}"


@pytest.mark.postgres
@pytest.mark.slow
def test_resource_id_is_as_wide_as_resource_type(test_postgres_url: str) -> None:
    """The migration widens resource_id to 255, matching resource_type."""
    if not alembic_available():
        pytest.skip("alembic CLI not on PATH")
    _upgrade_head(test_postgres_url)

    engine = create_engine(_sync_url(test_postgres_url))
    with engine.connect() as conn:
        widths = dict(
            conn.execute(
                text("""
            SELECT column_name, character_maximum_length
            FROM information_schema.columns
            WHERE table_name = 'audit_logs'
              AND column_name IN ('resource_id', 'resource_type')
        """)
            ).all()
        )
    engine.dispose()

    assert widths["resource_id"] == 255, (
        f"resource_id is varchar({widths['resource_id']}); a composite task id "
        "is 53+ chars and every audited task action is dropped"
    )
    assert widths["resource_id"] == widths["resource_type"]


@pytest.mark.postgres
@pytest.mark.slow
def test_composite_resource_id_actually_lands(
    test_postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real background writer must leave a ROW for a composite task id.

    On the unfixed schema the insert raises StringDataRightTruncationError,
    ``log_audit_event_bg`` catches it and logs a warning, and this SELECT comes
    back empty -- which is exactly what the live table showed.
    """
    if not alembic_available():
        pytest.skip("alembic CLI not on PATH")
    _upgrade_head(test_postgres_url)

    from server.database import engine as db_engine
    from server.database.models import AuditLog
    from server.services.audit_service import log_audit_event_bg
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    # The shape from the incident: "<project uuid>:<spec slug>", 53+ chars.
    resource_id = f"{uuid.uuid4()}:pending-{uuid.uuid4().hex[:8]}"
    assert len(resource_id) > 36

    async_engine = create_async_engine(test_postgres_url)
    session_factory = async_sessionmaker(async_engine, expire_on_commit=False)
    # log_audit_event_bg opens its own session via async_session_factory; the
    # patch target is the defining module because audit_service reads the
    # attribute live rather than binding it at import time.
    monkeypatch.setattr(db_engine, "async_session_factory", session_factory)

    async def _write_then_read() -> list[dict[str, Any]]:
        try:
            await log_audit_event_bg(
                action="mcp.task.create_and_run",
                resource_type="task",
                resource_id=resource_id,
            )
            async with session_factory() as session:
                rows = await session.execute(
                    select(AuditLog).where(AuditLog.resource_id == resource_id)
                )
                found = [
                    {"action": r.action, "resource_id": r.resource_id}
                    for r in rows.scalars()
                ]
                # Leave the shared acceptance DB as we found it. The downgrade
                # in this migration REFUSES while any resource_id exceeds 36
                # chars, so a row left behind here would make every later
                # `alembic downgrade` test in tests/postgres/ fail.
                await session.execute(
                    delete(AuditLog).where(AuditLog.resource_id == resource_id)
                )
                await session.commit()
                return found
        finally:
            # Dispose inside the same loop; a second asyncio.run() would close
            # asyncpg's connections against an already-closed event loop.
            await async_engine.dispose()

    found = asyncio.run(_write_then_read())

    assert len(found) == 1, (
        f"no audit row for resource_id {resource_id!r}: the write was rejected "
        "and log_audit_event_bg swallowed the error"
    )
    assert found[0]["action"] == "mcp.task.create_and_run"
    assert found[0]["resource_id"] == resource_id


@pytest.mark.postgres
@pytest.mark.slow
def test_migration_file_chains_onto_the_previous_head() -> None:
    """One head, and it is this migration -- a branched chain will not deploy."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config(str(Path(WEB_SERVER_ROOT) / "alembic.ini"))
    # script_location in alembic.ini is relative to apps/web-server; pytest runs
    # from the repo root, so make it absolute rather than depending on cwd.
    cfg.set_main_option(
        "script_location",
        str(Path(WEB_SERVER_ROOT) / "server" / "database" / "alembic"),
    )
    heads = ScriptDirectory.from_config(cfg).get_heads()
    assert heads == ["c1f5a3d7b924"], f"expected a single head, got {heads}"
