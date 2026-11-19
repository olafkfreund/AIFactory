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
from datetime import UTC, datetime
from pathlib import Path

from factory_common.logsafe import sanitize_log
from fastapi import APIRouter, Depends, HTTPException, Query, status

from server.project_registry import load_projects
from server.specpath import safe_spec_component

from .project_authz import require_task_access

router = APIRouter()
logger = logging.getLogger(__name__)


_DEFAULT_JOB_TAIL = 200


async def _job_log_entries(task_id: str, tail: int | None) -> list[dict[str, str]]:
    """Tail of the build Job's pod log for a kubejob task (#2144).

    Dispatch creates an empty ``task_logs.json`` and the packed path never ships
    the Job's own, so file logs are empty for the whole build. The pod is the
    source then. Best-effort: any failure returns ``[]``. The marker nonce is
    redacted exactly as the cockpit stream does (#1929).
    """
    from server.services.agent_service import get_agent_service  # noqa: PLC0415
    from server.services.build_log_stream import tail_job_logs  # noqa: PLC0415

    n = tail if isinstance(tail, int) and tail > 0 else _DEFAULT_JOB_TAIL
    try:
        svc = get_agent_service()
        if not getattr(svc, "_store_enabled", False):
            return []
        state = await svc._store().get_state(task_id)
        ref = (state or {}).get("worker_ref") or {}
        if ref.get("kind") != "k8s-job" or not ref.get("job_name"):
            return []
        lines = await tail_job_logs(ref["namespace"], ref["job_name"], n)
        stamp = datetime.now(UTC).isoformat()
        return [
            {
                "type": "job_log",
                "source": "job",
                "content": svc._redact_nonce(task_id, line),
                "timestamp": stamp,
            }
            for line in lines
        ]
    except Exception:  # noqa: BLE001 - logs are observability, never an error
        logger.debug("[GetTaskLogs] job log tail failed", exc_info=True)
        return []


@router.get("/{task_id}/logs")
async def get_task_logs(
    task_id: str,
    _access: dict = Depends(require_task_access("viewer")),
    tail: int | None = Query(default=None, ge=1, le=500),
):
    """Get logs for a task.

    Returns phase-based logs from task_logs.json if available,
    checking both main spec dir and worktree.
    """

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

            if not any((p or {}).get("entries") for p in result["phases"].values()):
                job_logs = await _job_log_entries(task_id, tail)
                if job_logs:
                    result["logs"] = job_logs
                    result["total"] = len(job_logs)
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
        except json.JSONDecodeError as e:
            logger.warning(
                f"[GetTaskLogs] Could not parse implementation_plan.json: {e}"
            )

    # QA report
    qa_report = spec_dir / "qa_report.md"
    if qa_report.exists():
        logs.append(
            {
                "type": "qa_report",
                "content": qa_report.read_text(),
                "timestamp": datetime.fromtimestamp(
                    qa_report.stat().st_mtime, UTC
                ).isoformat(),
            }
        )

    if not logs:
        logs = await _job_log_entries(task_id, tail)

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
