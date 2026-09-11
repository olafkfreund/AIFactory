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

Authorization (#319, tightened by #1554):

- Object-level scope, both endpoints: ``accessible_org_ids`` restricts the
  sweep to projects owned by an org the caller belongs to -- ``None``
  (service principal / local UI) means every project, same as everywhere
  else that helper is used -- mirroring ``tasks.py``'s ``list_tasks``.
- Tenant scope, both endpoints: when multi-tenant mode is on, ``sweep`` is
  additionally given the caller's resolved tenant and filters to specs
  stamped with it (mirrors ``list_tasks``'s ``spec_tenant`` filter -- org
  membership alone does not separate two tenants sharing one org).
- Role floor, WRITE only: a report (``GET``, or ``POST`` with
  ``dry_run=true``) stays at "any membership" (``accessible_org_ids``'s
  default), since it opens nothing. An actual sweep (``POST
  dry_run=false``) pushes branches and opens PRs -- the same side effect as
  ``routes/pr.py``'s ``create_pr_from_task``, which requires
  ``require_task_access("member")`` -- so it requires ``minimum_role=
  "member"`` in every org the sweep would touch, not mere viewer membership.

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
from server.tenancy import multi_tenant_enabled, resolve_tenant

router = APIRouter()

# Module-level singleton: a `Depends(get_db)` call directly in a default
# argument trips B008 (flake8-bugbear flags any call in a default position);
# assigning it once and referencing the name avoids that without changing
# FastAPI's resolution -- it still calls `get_db` per-request via the
# dependency graph, not once at import time.
_DB_DEP = Depends(get_db)


async def _visible_project_ids(
    request: Request, db: AsyncSession, *, minimum_role: str = "viewer"
) -> list[str]:
    """Project ids the caller may trigger the merger against (#319, #1554)."""
    projects = load_projects()
    allowed = await accessible_org_ids(request, db, minimum_role)
    if allowed is None:
        return list(projects.keys())
    return [pid for pid, p in projects.items() if p.get("org_id") in allowed]


def _tenant_scope(request: Request) -> str | None:
    """The caller's tenant to filter specs by, or None for "every tenant"
    (multi-tenant mode off -- #1554 finding 1)."""
    return resolve_tenant(request) if multi_tenant_enabled() else None


@router.get("/api/maintenance/merger")
async def report_merger(
    request: Request,
    db: AsyncSession = _DB_DEP,
) -> dict[str, Any]:
    """What the merger would do. Opens nothing."""
    project_ids = await _visible_project_ids(request, db)
    return await asyncio.to_thread(
        sweep, dry_run=True, project_ids=project_ids, tenant=_tenant_scope(request)
    )


@router.post("/api/maintenance/merger/run")
async def run_merger(
    request: Request,
    dry_run: bool = Query(
        True, description="default true: opening PRs must be asked for explicitly"
    ),
    db: AsyncSession = _DB_DEP,
) -> dict[str, Any]:
    """Open PRs for stranded task branches. Never merges; see services.merger."""
    # A real sweep pushes branches and opens PRs -- the same write the
    # per-task PR endpoint gates on "member" (#1554 finding 2). A dry run
    # opens nothing, so it stays at the report's "any membership" level.
    minimum_role = "viewer" if dry_run else "member"
    project_ids = await _visible_project_ids(request, db, minimum_role=minimum_role)
    return await asyncio.to_thread(
        sweep, dry_run=dry_run, project_ids=project_ids, tenant=_tenant_scope(request)
    )
