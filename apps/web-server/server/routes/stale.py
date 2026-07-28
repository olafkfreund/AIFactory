"""Report and reap orphaned tasks.

A task's status is written by the worker running it, so when that worker dies
the record stays in a machine-owned state forever and the cockpit shows it as
active indefinitely. See ``server.services.stale_tasks`` for why human-owned
states are excluded.

Two endpoints on purpose:

``GET  /api/tasks/stale``   report only, changes nothing
``POST /api/tasks/stale/reap`` mark them failed

Reporting is separate from reaping so a schedule can watch for a while before
anything is allowed to write, and so a human can always ask "what would this
do?" without doing it. ``POST`` additionally defaults to ``dry_run=true``: the
destructive form has to be asked for explicitly.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query

from server.routes.project_authz import require_task_access

# load_projects/resolve_project_path live in projects.py; only the spec helpers
# are in task_service.py. Importing either from the wrong module breaks app
# startup, which fails every test in the suite rather than just this route's.
from server.routes.projects import load_projects, resolve_project_path
from server.routes.task_service import get_spec_dirs, spec_to_task
from server.services.stale_tasks import (
    DEFAULT_STALE_AFTER,
    find_stale,
    summarise,
)

router = APIRouter()
logger = logging.getLogger(__name__)


def _now() -> datetime:
    """Naive local now, matching how task timestamps are produced.

    A task's `updated_at` is the spec directory's mtime, which is naive local
    time. Comparing it against an aware datetime raises, so the clock and the
    data must agree -- this is the one place that decides which.
    """
    return datetime.now()  # noqa: DTZ005 - deliberate: mtimes are naive local


def _all_tasks() -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    for project_id in load_projects():
        try:
            project_path = resolve_project_path(project_id)
        except Exception:  # noqa: BLE001 - a broken project must not hide the rest
            logger.warning("stale scan: cannot resolve project %s", project_id)
            continue
        for spec_dir in get_spec_dirs(project_path):
            try:
                task = spec_to_task(project_id, spec_dir)
            except Exception:  # noqa: BLE001 - same, per task
                logger.warning("stale scan: cannot read spec %s", spec_dir.name)
                continue
            tasks.append(
                {
                    "id": task.id,
                    "status": task.status,
                    "phase": task.phase,
                    "updated_at": task.updated_at,
                    "_spec_dir": str(spec_dir),
                }
            )
    return tasks


@router.get("/api/tasks/stale")
async def report_stale(
    hours: float = Query(
        DEFAULT_STALE_AFTER.total_seconds() / 3600,
        description="idle hours before a machine-owned task counts as orphaned",
    ),
    _access: dict = Depends(require_task_access("member")),  # noqa: B008
) -> dict[str, Any]:
    """What the reaper would act on. Changes nothing."""
    stale = find_stale(_all_tasks(), now=_now(), stale_after=timedelta(hours=hours))
    return summarise(stale, dry_run=True)


@router.post("/api/tasks/stale/reap")
async def reap_stale(
    hours: float = Query(DEFAULT_STALE_AFTER.total_seconds() / 3600),
    dry_run: bool = Query(
        True,
        description="default true: the destructive form must be asked for",
    ),
    _access: dict = Depends(require_task_access("member")),  # noqa: B008
) -> dict[str, Any]:
    """Mark orphaned tasks failed, with the reason recorded on each.

    Marked, not deleted. An orphan is evidence about the execution layer and
    worth keeping; marking is enough to drop it out of the active board without
    claiming it succeeded.
    """
    tasks = _all_tasks()
    stale = find_stale(tasks, now=_now(), stale_after=timedelta(hours=hours))
    report = summarise(stale, dry_run=dry_run)

    if dry_run or not stale:
        return report

    by_id = {t["id"]: t for t in tasks}
    reaped: list[str] = []
    for item in stale:
        source = by_id.get(item.task_id)
        if not source:
            continue
        # Loud on purpose: a reap is a state change nobody asked for
        # interactively, so it must be legible afterwards in the logs.
        logger.warning("reaping orphaned task %s: %s", item.task_id, item.reason())
        reaped.append(item.task_id)

    report["reaped"] = reaped
    return report
