"""Report and reap orphaned tasks.

A task's status is written by the worker running it, so when that worker dies
the record stays in a machine-owned state forever and the cockpit shows it as
active indefinitely. See ``server.services.stale_tasks`` for why human-owned
states are excluded.

Two endpoints on purpose:

``GET  /api/maintenance/stale-tasks``      report only, changes nothing
``POST /api/maintenance/stale-tasks/reap`` mark them failed

Under /api/maintenance rather than /api/tasks deliberately. `tasks.py` mounts
`@router.get("/{task_id}")` under an /api/tasks prefix, so /api/tasks/stale was
swallowed as a task id named "stale" and answered
`400 Invalid task ID format`. Registering this router first would also work,
but silently: any later reordering would re-break it with no test failing. A
path that cannot collide does not depend on anyone remembering.

Reporting is separate from reaping so a schedule can watch for a while before
anything is allowed to write, and so a human can always ask "what would this
do?" without doing it. ``POST`` additionally defaults to ``dry_run=true``: the
destructive form has to be asked for explicitly.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query

from server.error_ref import client_error
from server.project_registry import load_projects, resolve_project_path
from server.routes.task_service import get_spec_dirs, spec_to_task

# load_projects/resolve_project_path live in projects.py; only the spec helpers
# are in task_service.py. Importing either from the wrong module breaks app
# startup, which fails every test in the suite rather than just this route's.
from server.services.stale_tasks import (
    DEFAULT_STALE_AFTER,
    REAPED_STATUS,
    find_stale,
    summarise,
)
from server.services.task_status import write_status

# No route-level access dependency. `require_task_access` is task-scoped -- it
# reads a `task_id`, and on a path without one FastAPI promotes that to a
# REQUIRED QUERY PARAM, so the endpoint answered
# `422 {"loc":["query","task_id"],"msg":"Field required"}` and could never be
# called. These endpoints are not task-scoped; they scan every project.
#
# Authentication still applies: TokenAuthMiddleware covers everything outside
# PUBLIC_PREFIXES, verified against the live service (401 without a token, 200
# with one).
router = APIRouter()
logger = logging.getLogger(__name__)


def _now() -> datetime:
    """Naive local now, matching how task timestamps are produced.

    A task's `updated_at` is the spec directory's mtime, which is naive local
    time. Comparing it against an aware datetime raises, so the clock and the
    data must agree -- this is the one place that decides which.
    """
    return datetime.now()  # noqa: DTZ005 - deliberate: mtimes are naive local


# Status written to a reaped task. `cancelled` rather than `failed` for two
# checked reasons:
#   - AIFactory's own validator lists cancelled as valid and does NOT list
#     failed, and map_backend_status_to_frontend defaults anything unknown to
#     "backlog" -- so writing "failed" would put the orphan back in the backlog
#     column looking like fresh work.
#   - CFactory's is_failed() tokenises the status and matches "cancelled", so
#     the card lands in the cockpit's Failed tab and leaves Active.
# It also does not claim success, which "done" would.


def _mark_cancelled(spec_dir: Path, reason: str) -> None:
    """Persist the reap to both stores the board reads.

    Delegates to services.task_status so the reaper and the approve/merge path
    (#1071) cannot drift on what "write the status" means.
    """
    error = write_status(
        spec_dir,
        status=REAPED_STATUS,
        reason=reason,
        updated_by="stale-task-reaper",
    )
    if error:
        raise OSError(error)


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


def sweep(*, hours: float, dry_run: bool) -> dict[str, Any]:
    """Find orphans and, unless dry_run, cancel them. Returns the report.

    The HTTP route and the scheduled job both call this. Neither owns the
    logic, so a fix to one cannot leave the other behind -- which is the
    failure mode for anything that exists on both a manual and an automatic
    path.
    """
    tasks = _all_tasks()
    stale = find_stale(tasks, now=_now(), stale_after=timedelta(hours=hours))
    report = summarise(stale, dry_run=dry_run)

    if dry_run or not stale:
        return report

    by_id = {t["id"]: t for t in tasks}
    reaped: list[str] = []
    failures: list[str] = []
    for item in stale:
        source = by_id.get(item.task_id)
        if not source:
            continue
        spec_dir = Path(str(source["_spec_dir"]))
        # Loud on purpose: a reap is a state change nobody asked for
        # interactively, so it must be legible afterwards in the logs.
        logger.warning("reaping orphaned task %s: %s", item.task_id, item.reason())
        try:
            _mark_cancelled(spec_dir, item.reason())
        except OSError as exc:
            # Report the failure rather than counting it as reaped. A reaper
            # that says it acted and did not is the exact defect it exists to
            # find.
            logger.exception("could not reap %s", item.task_id)
            failures.append(
                f"{item.task_id}: {client_error(logger, 'could not mark the task cancelled', exc)}"
            )
            continue
        reaped.append(item.task_id)

    report["reaped"] = reaped
    if failures:
        report["failed_to_reap"] = failures
    return report


@router.get("/api/maintenance/stale-tasks")
async def report_stale(
    hours: float = Query(
        DEFAULT_STALE_AFTER.total_seconds() / 3600,
        description="idle hours before a machine-owned task counts as orphaned",
    ),
) -> dict[str, Any]:
    """What the reaper would act on. Changes nothing."""
    return sweep(hours=hours, dry_run=True)


@router.post("/api/maintenance/stale-tasks/reap")
async def reap_stale(
    hours: float = Query(DEFAULT_STALE_AFTER.total_seconds() / 3600),
    dry_run: bool = Query(
        True,
        description="default true: the destructive form must be asked for",
    ),
) -> dict[str, Any]:
    """Mark orphaned tasks cancelled, with the reason recorded on each.

    Marked, not deleted. An orphan is evidence about the execution layer and
    worth keeping; marking is enough to drop it out of the active board without
    claiming it succeeded.
    """
    return sweep(hours=hours, dry_run=dry_run)
