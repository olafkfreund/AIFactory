"""Task log endpoints — extracted from routes/tasks.py (#556 god-file split).

A focused sub-router carved out of routes/tasks.py; main.py mounts it under the
same /api/tasks prefix so paths are unchanged. Shared helpers/models stay in
routes/tasks.py and are imported here.

    GET  /api/tasks/{task_id}/logs
    POST /api/tasks/{task_id}/logs/watch
    POST /api/tasks/{task_id}/logs/unwatch
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from factory_common.logsafe import sanitize_log
from fastapi import APIRouter, Depends, HTTPException, status

from server.specpath import safe_spec_component

from ..project_store import load_projects
from .project_authz import require_task_access

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/{task_id}/logs")
async def get_task_logs(
    task_id: str, _access: dict = Depends(require_task_access("viewer"))
):
    """Get logs for a task.

    Returns phase-based logs from task_logs.json if available,
    checking both main spec dir and worktree.
    """
    import logging

    logger = logging.getLogger(__name__)

    logger.info(f"[GetTaskLogs] Called with task_id: {sanitize_log(task_id)}")

    if ":" not in task_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid task ID format",
        )

    project_id, spec_id = task_id.split(":", 1)

    # Barrier BEFORE spec_id reaches any path expression (#1056). Path joins
    # collapse traversal silently, so validating after the join is too late.
    try:
        spec_id = safe_spec_component(spec_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid task ID format",
        ) from None
    logger.info(
        f"[GetTaskLogs] project_id={sanitize_log(project_id)}, spec_id={sanitize_log(spec_id)}"
    )

    projects = load_projects()

    if project_id not in projects:
        logger.error(f"[GetTaskLogs] Project not found: {sanitize_log(project_id)}")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found",
        )

    project_path = Path(projects[project_id]["path"])
    logger.info(f"[GetTaskLogs] project_path: {project_path}")

    spec_dir = project_path / ".aifactory" / "specs" / spec_id
    worktree_spec_dir = (
        project_path
        / ".aifactory"
        / "worktrees"
        / "tasks"
        / spec_id
        / ".aifactory"
        / "specs"
        / spec_id
    )

    logger.info(f"[GetTaskLogs] Checking spec_dir: {sanitize_log(spec_dir)}")
    logger.info(
        f"[GetTaskLogs] Checking worktree_spec_dir: {sanitize_log(worktree_spec_dir)}"
    )

    # Check for task_logs.json (phase-based logs) - prefer worktree if exists
    task_logs_file = None
    for check_dir in [worktree_spec_dir, spec_dir]:
        candidate = check_dir / "task_logs.json"
        logger.info(f"[GetTaskLogs] Checking {candidate}, exists: {candidate.exists()}")
        if candidate.exists():
            task_logs_file = candidate
            logger.info(f"[GetTaskLogs] Found task_logs.json at: {task_logs_file}")
            break

    if task_logs_file:
        try:
            task_logs = json.loads(task_logs_file.read_text())
            logger.info(
                f"[GetTaskLogs] Successfully loaded task_logs.json, has phases: {'phases' in task_logs}"
            )
            result = {
                "specId": task_logs.get("spec_id", spec_id),
                "createdAt": task_logs.get("created_at"),
                "updatedAt": task_logs.get("updated_at"),
                "phases": task_logs.get("phases", {}),
            }

            # Also include build-progress.txt if it exists (detailed human-readable logs)
            for check_dir in [worktree_spec_dir, spec_dir]:
                build_progress = check_dir / "build-progress.txt"
                if build_progress.exists():
                    result["buildProgress"] = build_progress.read_text()
                    break

            logger.info(
                f"[GetTaskLogs] Returning phase-based logs with {len(result.get('phases', {}))} phases"
            )
            return result
        except json.JSONDecodeError as e:
            logger.error(f"[GetTaskLogs] JSON decode error: {e}")
            pass
    else:
        logger.warning(
            "[GetTaskLogs] No task_logs.json found, returning fallback format"
        )

    # Fallback: Collect logs from legacy sources
    logs = []

    # Implementation plan logs
    plan_file = spec_dir / "implementation_plan.json"
    if plan_file.exists():
        try:
            plan = json.loads(plan_file.read_text())
            if "logs" in plan:
                logs.extend(plan["logs"])
        except json.JSONDecodeError:
            pass

    # QA report
    qa_report = spec_dir / "qa_report.md"
    if qa_report.exists():
        logs.append(
            {
                "type": "qa_report",
                "content": qa_report.read_text(),
                "timestamp": datetime.fromtimestamp(
                    qa_report.stat().st_mtime
                ).isoformat(),
            }
        )

    result = {"logs": logs, "total": len(logs)}

    # Include build-progress.txt if it exists
    for check_dir in [worktree_spec_dir, spec_dir]:
        build_progress = check_dir / "build-progress.txt"
        if build_progress.exists():
            result["buildProgress"] = build_progress.read_text()
            break

    return result


@router.post("/{task_id}/logs/watch")
async def watch_task_logs(
    task_id: str, _access: dict = Depends(require_task_access("viewer"))
):
    """
    Start watching task logs (stub endpoint for frontend compatibility).

    Note: Log streaming is handled via WebSocket, this endpoint is a no-op
    that prevents 404 errors in the frontend.
    """
    return {"success": True, "message": "Log watching handled via WebSocket"}


@router.post("/{task_id}/logs/unwatch")
async def unwatch_task_logs(
    task_id: str, _access: dict = Depends(require_task_access("viewer"))
):
    """
    Stop watching task logs (stub endpoint for frontend compatibility).

    Note: Log streaming is handled via WebSocket, this endpoint is a no-op
    that prevents 404 errors in the frontend.
    """
    return {"success": True, "message": "Log unwatching handled via WebSocket"}
