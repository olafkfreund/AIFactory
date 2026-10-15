"""Durable copy of the TFactory handback receipt (#1696).

The QA Fixer can delete handback_received.json, so the merge gate also reads this row.
"""

from __future__ import annotations

import logging
from pathlib import Path

from server.database.models import HandbackReceipt
from server.services.trusted_contract_store import spec_key_for_dir

logger = logging.getLogger(__name__)


async def record(spec_dir: str | Path) -> None:
    try:
        from server.services.job_state_store import store_enabled  # noqa: PLC0415

        if not store_enabled():
            return
        from server.database.engine import async_session_factory  # noqa: PLC0415

        spec_key = spec_key_for_dir(spec_dir)
        async with async_session_factory() as session:
            if await session.get(HandbackReceipt, spec_key) is None:
                session.add(
                    HandbackReceipt(spec_key=spec_key, spec_id=Path(spec_dir).name)
                )
                await session.commit()
    except Exception:  # noqa: BLE001 - receipt errors must not interrupt handback
        logger.error("[handback-receipt] record failed")


async def on_record(spec_dir: str | Path) -> bool:
    try:
        from server.services.job_state_store import store_enabled  # noqa: PLC0415

        if not store_enabled():
            return False
        from server.database.engine import async_session_factory  # noqa: PLC0415

        async with async_session_factory() as session:
            return (
                await session.get(HandbackReceipt, spec_key_for_dir(spec_dir))
                is not None
            )
    except Exception:  # noqa: BLE001 - a failed read must hold the merge
        logger.error("[handback-receipt] lookup failed; auto-merge withheld")
        return True
