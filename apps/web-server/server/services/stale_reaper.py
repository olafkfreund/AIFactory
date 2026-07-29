"""Sweep orphaned tasks on a schedule (#1064).

A task's status is written by the worker running it, so when that worker dies
nothing writes the terminal status and the task shows as active forever. Doing
this by hand means someone has to notice first, and nobody notices a card that
looks busy -- ``089-memory-chain-probe-three`` sat at ``in_progress`` for 36
hours in plain sight.

## Why a lifespan task and not a CronJob

The obvious shape was a CronJob, matching the audit anchor. It would not work.
Task specs live on ``aifactory-data``, a ReadWriteOnce local-path PVC, so a
separate pod only sees them if the scheduler happens to place it on the node
holding the volume. Placed anywhere else it finds an empty directory, reports
zero orphans and exits 0 -- a green scheduled job proving nothing, which is the
failure mode this whole feature exists to detect. Running inside the web pod
means the loop reads the same filesystem the API reads, by construction.

Multiple replicas would each sweep, which is harmless: reaping is idempotent
because ``cancelled`` is terminal, so a second sweep finds nothing to do.

## Report-only by default

``REAPER_DRY_RUN`` defaults to true, so enabling the loop changes nothing until
someone decides otherwise. The point of the first weeks is to read what it
*would* have done and confirm it only ever names real orphans. Set
``REAPER_DRY_RUN=false`` to let it write.

The loop itself is off unless ``AIFACTORY_STALE_REAPER`` is set, matching the
intake poller and outbox relay.

Run one sweep by hand, inside the pod:

    python -m server.services.stale_reaper
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from typing import Any

from server.routes.stale import sweep
from server.services.stale_tasks import DEFAULT_STALE_AFTER

logger = logging.getLogger(__name__)

_DEFAULT_HOURS = DEFAULT_STALE_AFTER.total_seconds() / 3600
_DEFAULT_INTERVAL_S = 1800.0


def reaper_enabled() -> bool:
    """Off unless explicitly switched on, like the other lifespan loops."""
    return os.environ.get("AIFACTORY_STALE_REAPER", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def dry_run() -> bool:
    """Anything other than an explicit "false" means report-only.

    Fails closed on a typo: ``REAPER_DRY_RUN=flase`` reports rather than
    writes. The cost of getting this wrong is asymmetric -- a typo that
    reports wastes a tick, a typo that writes cancels live tasks.
    """
    return os.environ.get("REAPER_DRY_RUN", "true").strip().lower() != "false"


def _positive_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("%s=%r is not a number; using %s", name, raw, default)
        return default
    if value <= 0:
        # Zero would make every machine-owned task 'idle longer than 0h' and
        # reap the whole board; a zero interval would spin. Neither is a
        # plausible intent, so treat both as a misconfiguration.
        logger.warning("%s=%r must be positive; using %s", name, raw, default)
        return default
    return value


def stale_hours() -> float:
    return _positive_float("REAPER_STALE_HOURS", _DEFAULT_HOURS)


def interval_s() -> float:
    return _positive_float("REAPER_INTERVAL_SECONDS", _DEFAULT_INTERVAL_S)


def sweep_once() -> dict[str, Any]:
    """One pass. Returns the report and logs it as a single JSON line.

    The log IS the evidence of what a dry run would have done, which is the
    whole value of the report-only phase. The whole report rather than a
    count, because "3 stale" tells an operator nothing about whether it was
    right.
    """
    report = sweep(hours=stale_hours(), dry_run=dry_run())
    logger.info("stale-reaper %s", json.dumps(report, sort_keys=True))
    if report.get("failed_to_reap"):
        logger.error("stale-reaper could not reap: %s", report["failed_to_reap"])
    return report


async def reaper_loop(*, stop: asyncio.Event | None = None) -> None:
    """One sweep per interval until stopped. A failed tick never kills it."""
    stop = stop or asyncio.Event()
    interval = interval_s()
    while not stop.is_set():
        try:
            # The sweep walks the spec tree and writes files -- blocking work,
            # so it must not run on the event loop and stall every request.
            await asyncio.to_thread(sweep_once)
        except Exception:
            logger.exception("stale-reaper tick failed; continuing")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            continue


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    report = sweep_once()
    # A reap that could not write must not exit 0. A green run while orphans
    # pile up is the failure this feature exists for.
    return 1 if report.get("failed_to_reap") else 0


if __name__ == "__main__":
    sys.exit(main())
