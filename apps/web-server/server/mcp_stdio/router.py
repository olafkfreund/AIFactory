"""Proxy router for the stdio-MCP control plane (Issue #154).

Re-exposes the 15 operations the stdio MCP client exercises today,
under ``/api/mcp-stdio/*`` — each route gated by ``acw_`` key + scope.

Every handler delegates to the same service the regular REST routes
use. The proxy adds nothing but auth: no URL rewriting, no payload
transformation, no caching. If a delegated handler raises
``HTTPException``, it propagates as-is.

Why import-and-call instead of HTTP-forwarding to the existing routes:
- One process, no extra hop, no risk of an MCP request looping back
  through ``TokenAuthMiddleware`` which would reject the ``acw_`` key
  on the regular REST surface.
- Same code path, so behavior identity is mechanical.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Query
from fastapi import Request as FastAPIRequest

from .auth import (
    MCP_READ_SCOPE,
    PROJECT_WRITE_SCOPE,
    TASK_MERGE_SCOPE,
    TASK_WRITE_SCOPE,
    require_acw_scope,
)

router = APIRouter(prefix="/api/mcp-stdio", tags=["MCP (stdio)"])


async def _read_json_body(request: FastAPIRequest) -> dict:
    """Read request body, return {} if empty. The stdio MCP client
    posts ``{}`` for arg-less mutations, so handlers that need no
    body get an empty dict and instantiate defaults.
    """
    raw = await request.body()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


# =============================================================================
# Read operations — mcp:read
# =============================================================================

@router.get("/projects")
async def proxy_list_projects(_=Depends(require_acw_scope(MCP_READ_SCOPE))):
    from ..routes.projects import list_projects
    return await list_projects()


@router.get("/tasks")
async def proxy_list_tasks(
    project_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
    _=Depends(require_acw_scope(MCP_READ_SCOPE)),
):
    from ..routes.tasks import list_tasks
    return await list_tasks(project_id=project_id, status=status)


@router.get("/tasks/running")
async def proxy_get_running_tasks(_=Depends(require_acw_scope(MCP_READ_SCOPE))):
    from ..routes.execution import get_running_tasks
    return await get_running_tasks()


@router.get("/tasks/{task_id}")
async def proxy_get_task(task_id: str, _=Depends(require_acw_scope(MCP_READ_SCOPE))):
    from ..routes.tasks import get_task
    return await get_task(task_id)


@router.get("/tasks/{task_id}/status")
async def proxy_get_task_status(task_id: str, _=Depends(require_acw_scope(MCP_READ_SCOPE))):
    from ..routes.execution import get_task_status
    return await get_task_status(task_id)


@router.get("/tasks/{task_id}/logs")
async def proxy_get_task_logs(
    task_id: str,
    tail: int = Query(default=100),
    _=Depends(require_acw_scope(MCP_READ_SCOPE)),
):
    from ..routes.tasks import get_task_logs
    return await get_task_logs(task_id, tail=tail)


@router.get("/tasks/{task_id}/worktree/diff")
async def proxy_get_worktree_diff(task_id: str, _=Depends(require_acw_scope(MCP_READ_SCOPE))):
    from ..routes.tasks import get_worktree_diff
    return await get_worktree_diff(task_id)


# =============================================================================
# Project mutation — project:write
# =============================================================================

@router.post("/projects", status_code=201)
async def proxy_add_project(
    request: FastAPIRequest,
    _=Depends(require_acw_scope(PROJECT_WRITE_SCOPE)),
):
    from ..routes.projects import ProjectCreate, add_project
    body = await _read_json_body(request)
    return await add_project(ProjectCreate(**body))


# =============================================================================
# Task mutation — task:write
# =============================================================================

@router.post("/tasks/create-and-run")
async def proxy_create_and_run_task(
    request: FastAPIRequest,
    project_id: str = Query(...),
    title: str = Query(...),
    description: str = Query(...),
    _=Depends(require_acw_scope(TASK_WRITE_SCOPE)),
):
    from ..routes.execution import StartTaskRequest, create_and_run_task
    body = await _read_json_body(request)
    return await create_and_run_task(
        project_id, title, description, StartTaskRequest(**body)
    )


@router.post("/tasks/{task_id}/start")
async def proxy_start_task(
    task_id: str,
    request: FastAPIRequest,
    _=Depends(require_acw_scope(TASK_WRITE_SCOPE)),
):
    from ..routes.execution import StartTaskRequest, start_task
    body = await _read_json_body(request)
    return await start_task(task_id, StartTaskRequest(**body), request)


@router.post("/tasks/{task_id}/stop")
async def proxy_stop_task(task_id: str, _=Depends(require_acw_scope(TASK_WRITE_SCOPE))):
    from ..routes.execution import stop_task
    return await stop_task(task_id)


@router.post("/tasks/{task_id}/recover")
async def proxy_recover_task(
    task_id: str,
    request: FastAPIRequest,
    _=Depends(require_acw_scope(TASK_WRITE_SCOPE)),
):
    from ..routes.execution import RecoverTaskRequest, recover_task
    body = await _read_json_body(request)
    return await recover_task(task_id, RecoverTaskRequest(**body))


@router.post("/tasks/{task_id}/approve-plan")
async def proxy_approve_plan(
    task_id: str,
    request: FastAPIRequest,
    _=Depends(require_acw_scope(TASK_WRITE_SCOPE)),
):
    from ..routes.tasks import ApprovePlanRequest, approve_plan
    body = await _read_json_body(request)
    return await approve_plan(task_id, ApprovePlanRequest(**body))


# =============================================================================
# PR / merge — task:merge (higher blast radius)
# =============================================================================

@router.post("/tasks/{task_id}/worktree/create-pr")
async def proxy_create_pr(
    task_id: str,
    request: FastAPIRequest,
    _=Depends(require_acw_scope(TASK_MERGE_SCOPE)),
):
    from ..routes.tasks import CreatePRFromTaskOptions, create_pr_from_task
    body = await _read_json_body(request)
    return await create_pr_from_task(
        task_id, CreatePRFromTaskOptions(**body) if body else None
    )


@router.post("/tasks/{task_id}/worktree/merge")
async def proxy_merge_worktree(
    task_id: str,
    request: FastAPIRequest,
    _=Depends(require_acw_scope(TASK_MERGE_SCOPE)),
):
    from ..routes.tasks import WorktreeMergeOptions, merge_worktree
    body = await _read_json_body(request)
    return await merge_worktree(
        task_id, WorktreeMergeOptions(**body) if body else None
    )
