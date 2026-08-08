"""Task usage endpoints — extracted from routes/tasks.py (#556 god-file split).

Token-usage and process resource-usage views carved out of routes/tasks.py;
tasks.py re-mounts this sub-router via router.include_router so the public
/api/tasks paths are unchanged. Shared helpers/models stay in routes/tasks.py
and are imported here.

    GET /api/tasks/{task_id}/token-usage
    GET /api/tasks/{task_id}/resource-usage
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status

from .project_authz import require_task_access

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/{task_id}/token-usage")
async def get_task_token_usage(
    task_id: str, _access: dict = Depends(require_task_access("viewer"))
):
    """Per-category token / cost breakdown for a task's session(s) (#262).

    Returns the structured breakdown produced by the backend token-attribution
    module: each source category (system/CLAUDE.md instructions, user messages,
    team/coordination context, tool outputs, thinking+output) with its token
    count, %-of-context-window and apportioned $ cost, plus session totals.

    Reads the agent-written ``token_usage.json`` from the main spec dir (the
    agent loop syncs it back from the worktree). Returns an empty (all-zero)
    breakdown when no session has run yet — never 404 on a valid task, so the
    UI can render a stable empty state.
    """
    # Lazy import to avoid a circular import (tasks.py mounts this sub-router).
    from .tasks import _resolve_task, get_worktree_spec_dir

    project_id, spec_id, project_path, spec_dir = _resolve_task(task_id)

    # Prefer the main spec dir (synced from worktree). Fall back to the live
    # worktree spec dir if the sync hasn't landed yet.
    candidate = spec_dir / "token_usage.json"
    if not candidate.exists():
        worktree_spec_dir = get_worktree_spec_dir(project_path, spec_id)
        if worktree_spec_dir and (worktree_spec_dir / "token_usage.json").exists():
            spec_dir = worktree_spec_dir

    # Import the backend attribution reader (sys.path shim, same approach as
    # reject_plan above — web-server doesn't always have backend on PYTHONPATH).
    import sys

    backend_path = Path(__file__).parent.parent.parent.parent / "backend"
    if str(backend_path) not in sys.path:
        sys.path.insert(0, str(backend_path))

    try:
        from agents.token_attribution import read_breakdown
    except ImportError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Token attribution module unavailable: {exc}",
        ) from exc

    return read_breakdown(spec_dir)


def _not_running_resource_usage() -> dict:
    """The point-in-time resource shape when no agent process is active.

    Used both when the task has no live subprocess and as the degraded
    fallback whenever sampling fails (dead/missing PID, psutil error, etc.).
    """
    return {
        "running": False,
        "pid": None,
        "cpuPercent": 0.0,
        "memoryMb": 0.0,
        "memoryPercent": 0.0,
        "sampledAt": datetime.now(UTC).isoformat(),
    }


def _sample_process_resources(pid: int) -> dict:
    """Sample CPU%/RAM for ``pid`` (and its children) using psutil.

    Returns the populated resource shape, or the not-running shape if the
    PID is gone or sampling raises for any reason. Never propagates an
    exception — the endpoint must degrade gracefully, not 500.

    CPU% is measured with a short blocking ``interval`` so a single
    point-in-time poll yields a meaningful number (psutil's non-blocking
    mode returns 0.0 on the first call for a process it hasn't seen). The
    parent's and children's percentages are summed so multi-process agent
    runs (e.g. a CLI that spawns workers) report aggregate load.
    """
    try:
        import psutil
    except ImportError:
        # psutil not installed — degrade rather than crash the endpoint.
        return _not_running_resource_usage()

    try:
        proc = psutil.Process(pid)

        # Gather parent + children once so we can sum CPU and RSS.
        procs = [proc]
        try:
            procs.extend(proc.children(recursive=True))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

        # Prime CPU counters, then sample over a short interval. Priming on
        # the parent is enough for it; children are primed implicitly by the
        # first read and summed best-effort (0.0 on their first read is
        # acceptable for a point-in-time poll the frontend repeats).
        cpu_percent = 0.0
        memory_mb = 0.0
        for p in procs:
            try:
                if p.pid == pid:
                    cpu_percent += p.cpu_percent(interval=0.1)
                else:
                    cpu_percent += p.cpu_percent(interval=None)
                memory_mb += p.memory_info().rss / (1024 * 1024)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                # A child died mid-sample; skip it.
                continue

        # System RAM percentage from the aggregate RSS.
        try:
            total_ram = psutil.virtual_memory().total
            memory_percent = (
                (memory_mb * 1024 * 1024 / total_ram) * 100 if total_ram else 0.0
            )
        except Exception:
            memory_percent = 0.0

        return {
            "running": True,
            "pid": pid,
            "cpuPercent": round(cpu_percent, 1),
            "memoryMb": round(memory_mb, 1),
            "memoryPercent": round(memory_percent, 2),
            "sampledAt": datetime.now(UTC).isoformat(),
        }
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        # PID gone or inaccessible between the running-check and the sample.
        return _not_running_resource_usage()
    except Exception:
        # Belt-and-braces: any unexpected psutil/OS error degrades cleanly.
        return _not_running_resource_usage()


@router.get("/{task_id}/resource-usage")
async def get_task_resource_usage(
    task_id: str, _access: dict = Depends(require_task_access("viewer"))
):
    """Point-in-time CPU/RAM of the running agent subprocess for a task (#277).

    The frontend polls this to drive a live per-agent resource panel. Returns
    raw JSON (the api-client wraps it):

        {
          "running": bool,        # is an agent process active for this task
          "pid": int | null,
          "cpuPercent": number,   # process CPU% (parent + children), 0.0 when idle
          "memoryMb": number,     # aggregate RSS in MB
          "memoryPercent": number,# % of system RAM
          "sampledAt": str        # ISO-8601 UTC
        }

    Behaviour:
      - Unknown task → 404 (via ``_resolve_task``).
      - Valid task with no live process → the not-running shape (never 404).
      - Sampling is failure-safe: a dead/missing PID or psutil error degrades
        to the not-running shape rather than raising.
    """
    # 404 only for an unknown task (bad format / missing project / missing spec).
    # Lazy import to avoid a circular import (tasks.py mounts this sub-router).
    from .tasks import _resolve_task

    _resolve_task(task_id)

    from ..services.agent_service import get_agent_service

    agent_service = get_agent_service()

    # running_tasks maps task_id -> asyncio.subprocess.Process; .pid is the
    # OS pid of the spawned agent CLI. Absence means no live agent process.
    proc = agent_service.running_tasks.get(task_id)
    if proc is None or proc.pid is None:
        return _not_running_resource_usage()

    # If the process object exists but has already exited, returncode is set —
    # treat that as not-running too rather than sampling a reaped pid.
    if getattr(proc, "returncode", None) is not None:
        return _not_running_resource_usage()

    return _sample_process_resources(proc.pid)
