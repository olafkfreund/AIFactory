"""Open PRs for finished-but-stranded task branches.

See ``server.services.merger`` for why this exists: work lands, gets pushed,
and QA correctly refuses to approve it without gate evidence (#1496) -- so
``services.pr_endgame``'s "open a PR as a side-effect of a clean build" never
fires, and the branch just sits there. This is the other half: an idempotent
sweep, invocable on demand, that finds every such branch and opens a PR for it
regardless of QA status -- honestly labelled as unverified where it is.

Two endpoints, same shape as ``routes/stale.py`` on purpose:

``GET  /api/maintenance/merger``      report only, opens nothing
``POST /api/maintenance/merger/run``  opens PRs; defaults to dry_run=true

Under ``/api/maintenance`` rather than ``/api/tasks`` for the same reason
``stale.py`` is: this scans every project, it is not task-scoped, so it must
not collide with ``tasks.py``'s ``@router.get("/{task_id}")``.

Authorization mirrors ``tasks.py``'s ``list_tasks`` (#319): the sweep is
restricted to projects owned by an org the caller belongs to via
``accessible_org_ids`` -- ``None`` (service principal / local UI) means every
project, same as everywhere else that helper is used. This is object-level
scoping, not a route-level role gate, because the merger's own purpose --
opening PRs for a caller's own stranded work -- does not call for an
admin-only endpoint; it calls for the same "see only your orgs" rule every
other fleet-wide read in this router already applies.

Runs ``sweep`` in a worker thread via ``asyncio.to_thread``: it shells out to
``git``/``gh`` per spec examined, and running that on the event loop would
stall every other request and WebSocket for the scan's whole duration.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from server.database.engine import get_db
from server.project_registry import load_projects
from server.routes.project_authz import accessible_org_ids
from server.services.merger import sweep

router = APIRouter()

# Module-level singleton: a `Depends(get_db)` call directly in a default
# argument trips B008 (flake8-bugbear flags any call in a default position);
# assigning it once and referencing the name avoids that without changing
# FastAPI's resolution -- it still calls `get_db` per-request via the
# dependency graph, not once at import time.
_DB_DEP = Depends(get_db)


async def _visible_project_ids(request: Request, db: AsyncSession) -> list[str]:
    """Project ids the caller may trigger the merger against (#319)."""
    projects = load_projects()
    allowed = await accessible_org_ids(request, db)
    if allowed is None:
        return list(projects.keys())
    return [pid for pid, p in projects.items() if p.get("org_id") in allowed]


@router.get("/api/maintenance/merger")
async def report_merger(
    request: Request,
    db: AsyncSession = _DB_DEP,
) -> dict[str, Any]:
    """What the merger would do. Opens nothing."""
    project_ids = await _visible_project_ids(request, db)
    return await asyncio.to_thread(sweep, dry_run=True, project_ids=project_ids)


@router.post("/api/maintenance/merger/run")
async def run_merger(
    request: Request,
    dry_run: bool = Query(
        True, description="default true: opening PRs must be asked for explicitly"
    ),
    db: AsyncSession = _DB_DEP,
) -> dict[str, Any]:
    """Open PRs for stranded task branches. Never merges; see services.merger."""
    project_ids = await _visible_project_ids(request, db)
    return await asyncio.to_thread(sweep, dry_run=dry_run, project_ids=project_ids)
