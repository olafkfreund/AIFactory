"""Durable record of the PFactory-signed task contract (#1667).

The coding agent can edit ``context/task_contract.json``, so the merge gate
decides from the row written here at ingest. ``build_isolation`` is stamped by
the web server at spawn and is sticky once ``none``.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import or_, select, update

from server.database.models import TrustedContract

logger = logging.getLogger(__name__)

# Returned by lookup() when the database could not be read: callers must hold.
LOOKUP_FAILED: Any = object()


@dataclass(frozen=True)
class TrustedRecord:
    contract: dict[str, Any]
    build_isolation: str | None


def spec_key_for_dir(spec_dir: str | Path) -> str:
    return hashlib.sha256(str(Path(spec_dir).resolve()).encode()).hexdigest()


class TrustedContractStore:
    def __init__(self, session_factory: Any = None) -> None:
        if session_factory is None:
            # Lazy import keeps non-DB code paths clean (same as JobStateStore).
            from server.database.engine import async_session_factory  # noqa: PLC0415

            session_factory = async_session_factory
        self._session_factory = session_factory

    async def put(self, spec_key: str, spec_id: str, contract: dict[str, Any]) -> None:
        """Upsert. A changed contract is a new build, so the isolation stamp resets."""
        text = json.dumps(contract)
        async with self._session_factory() as session:
            row = await session.get(TrustedContract, spec_key)
            if row is None:
                session.add(
                    TrustedContract(spec_key=spec_key, spec_id=spec_id, contract=text)
                )
            else:
                if row.contract != text:
                    row.build_isolation = None
                row.contract = text
                row.spec_id = spec_id
            await session.commit()

    async def get(self, spec_key: str) -> TrustedRecord | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(TrustedContract).where(TrustedContract.spec_key == spec_key)
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return TrustedRecord(json.loads(row.contract), row.build_isolation)

    async def stamp_isolation(self, spec_key: str, kind: str) -> None:
        """Set the stamp; no-op on a missing row; ``none`` is never overwritten.

        One conditional UPDATE, so two concurrent spawns cannot race ``none`` away.
        """
        async with self._session_factory() as session:
            await session.execute(
                update(TrustedContract)
                .where(TrustedContract.spec_key == spec_key)
                .where(
                    or_(
                        TrustedContract.build_isolation.is_(None),
                        TrustedContract.build_isolation != "none",
                    )
                )
                .values(build_isolation=kind)
            )
            await session.commit()


async def lookup(spec_dir: str | Path) -> TrustedRecord | None | Any:
    """The record for *spec_dir*, ``None`` if absent, ``LOOKUP_FAILED`` on any error."""
    try:
        return await TrustedContractStore().get(spec_key_for_dir(spec_dir))
    except Exception:  # noqa: BLE001 - a failed read must hold, never raise
        logger.error("[trusted-contract] record lookup failed; auto-merge withheld")
        return LOOKUP_FAILED


async def stamp_spawn(spec_dir: str | Path, kind: str) -> None:
    """Stamp how a build is isolated.

    A failed isolated stamp is logged and the build goes on: the stamp stays
    empty (holds) or keeps an earlier isolated value. A failed ``none`` stamp
    raises, because an earlier isolated stamp would otherwise survive a build
    that was not isolated.
    """
    try:
        await TrustedContractStore().stamp_isolation(spec_key_for_dir(spec_dir), kind)
    except Exception:
        logger.error("[trusted-contract] isolation stamp not written")
        if kind == "none":
            raise
