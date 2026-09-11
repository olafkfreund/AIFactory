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
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from server.services.merger import sweep

router = APIRouter()


@router.get("/api/maintenance/merger")
async def report_merger() -> dict[str, Any]:
    """What the merger would do. Opens nothing."""
    return sweep(dry_run=True)


@router.post("/api/maintenance/merger/run")
async def run_merger(
    dry_run: bool = Query(
        True, description="default true: opening PRs must be asked for explicitly"
    ),
) -> dict[str, Any]:
    """Open PRs for stranded task branches. Never merges; see services.merger."""
    return sweep(dry_run=dry_run)
