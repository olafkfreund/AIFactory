"""Postgres store for read-only investigation runs (#2156).

Row access only: no Kubernetes, no policy. ``lock`` is the ``SELECT ... FOR
UPDATE`` the lifecycle code holds across its Kubernetes calls.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select, update as sa_update
from sqlalchemy.exc import IntegrityError

from server.database.engine import async_session_factory
from server.database.models import Investigation, InvestigationLog

MAX_LOG_LIMIT = 500


class SlotTaken(Exception):  # noqa: N818 - Public exception name used by callers.
    """The project already has a run whose pods are not confirmed gone."""


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)  # naive UTC, like the schema


class InvestigationStore:
    def __init__(self, session_factory: Any = None) -> None:
        if session_factory is None:
            session_factory = async_session_factory
        self._session_factory = session_factory

    async def insert(self, **fields: Any) -> Any:
        """Insert a run; raises ``SlotTaken`` when the project's slot is held."""
        async with self._session_factory() as session:
            session.add(Investigation(**fields))
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                if "uq_investigations_project_slot" in str(exc):
                    raise SlotTaken(fields.get("project_id")) from exc
                raise
            return await session.get(Investigation, fields["id"])

    async def get(self, run_id: str) -> Any | None:
        async with self._session_factory() as session:
            return await session.get(Investigation, run_id)

    @asynccontextmanager
    async def lock(self, run_id: str) -> AsyncIterator[tuple[Any, Any]]:
        """``SELECT ... FOR UPDATE`` one run; yields ``(session, row)``.

        Commits on a clean exit and rolls back on an exception. ``row`` is
        None when the id does not exist. ``update(..., session=session)``
        writes inside the same transaction.
        """
        async with self._session_factory() as session:
            stmt = select(Investigation).where(Investigation.id == run_id)
            if session.bind.dialect.name != "sqlite":
                stmt = stmt.with_for_update()
            row = (await session.execute(stmt)).scalar_one_or_none()
            try:
                yield session, row
            except BaseException:
                await session.rollback()
                raise
            await session.commit()

    async def update(self, run_id: str, *, session: Any = None, **fields: Any) -> None:
        stmt = (
            sa_update(Investigation).where(Investigation.id == run_id).values(**fields)
        )
        if session is not None:
            await session.execute(stmt)
            return
        async with self._session_factory() as own:
            await own.execute(stmt)
            await own.commit()

    async def append_logs(self, run_id: str, lines: list[str]) -> None:
        if not lines:
            return
        now = _now()
        async with self._session_factory() as session:
            session.add_all(
                InvestigationLog(run_id=run_id, at=now, line=ln) for ln in lines
            )
            await session.commit()

    async def read_logs(
        self, run_id: str, after: int = 0, limit: int = 100
    ) -> dict[str, Any]:
        """``{entries:[{seq,at,line}], cursor}``; cursor is ``after`` when idle."""
        limit = max(1, min(limit, MAX_LOG_LIMIT))
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(InvestigationLog)
                    .where(
                        InvestigationLog.run_id == run_id,
                        InvestigationLog.seq > after,
                    )
                    .order_by(InvestigationLog.seq)
                    .limit(limit)
                )
            ).scalars()
            entries = [
                {"seq": r.seq, "at": r.at.isoformat(), "line": r.line} for r in rows
            ]
        return {
            "entries": entries,
            "cursor": entries[-1]["seq"] if entries else after,
        }

    async def live_runs(self, limit: int = 50) -> list[Any]:
        """Runs whose pods are not confirmed gone, oldest first."""
        async with self._session_factory() as session:
            return list(
                (
                    await session.execute(
                        select(Investigation)
                        .where(Investigation.pods_gone.is_(False))
                        .order_by(Investigation.created_at)
                        .limit(limit)
                    )
                ).scalars()
            )

    async def purge_logs(self, older_than: datetime) -> int:
        """Delete log rows of runs that ended before ``older_than``."""
        async with self._session_factory() as session:
            res = await session.execute(
                delete(InvestigationLog).where(
                    InvestigationLog.run_id.in_(
                        select(Investigation.id).where(
                            Investigation.ended_at.is_not(None),
                            Investigation.ended_at < older_than,
                        )
                    )
                )
            )
            await session.commit()
            return res.rowcount or 0
