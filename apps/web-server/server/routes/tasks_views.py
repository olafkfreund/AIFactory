"""Task read-view endpoints — extracted from routes/tasks.py (#556 split).

Read-only task views (qa-report, agent-console SSE, plan-html) carved out of
routes/tasks.py; tasks.py re-mounts this sub-router via router.include_router so
the public /api/tasks paths are unchanged. Shared helpers/models stay in
routes/tasks.py and are imported here.

    GET /api/tasks/{task_id}/qa-report
    GET /api/tasks/{task_id}/agent-console/sse
    GET /api/tasks/{task_id}/plan-html
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status

from .project_authz import require_task_access
from .projects import load_projects

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/{task_id}/qa-report")
async def get_qa_report(
    task_id: str, _access: dict = Depends(require_task_access("viewer"))
):
    """Return the QA report markdown for a task.

    Tasks that have completed the QA phase have a ``qa_report.md`` written
    to their spec dir. This endpoint surfaces that content + a few derived
    fields so an MCP client can show it inline without separately reading
    the filesystem.
    """
    if ":" not in task_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid task ID format"
        )

    project_id, spec_id = task_id.split(":", 1)
    projects = load_projects()
    if project_id not in projects:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Project not found"
        )

    project_path = Path(projects[project_id]["path"])
    spec_dir = project_path / ".aifactory" / "specs" / spec_id
    qa_report_file = spec_dir / "qa_report.md"

    if not qa_report_file.exists():
        # 404 is the right answer — clients should treat "no report yet"
        # as "task hasn't reached QA" rather than a hard error.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="QA report not found — task may not have reached the QA phase yet",
        )

    try:
        content = qa_report_file.read_text(encoding="utf-8")
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Could not read QA report: {exc}",
        ) from exc

    return {
        "task_id": task_id,
        "spec_id": spec_id,
        "exists": True,
        "size_bytes": qa_report_file.stat().st_size,
        "modified_at": qa_report_file.stat().st_mtime,
        "content": content,
    }


@router.get("/{task_id}/agent-console/sse")
async def stream_agent_console(
    task_id: str, _access: dict = Depends(require_task_access("viewer"))
):
    """Server-Sent Events stream of the running agent's console output.

    V1.1 strategy: read ``build-progress.txt`` from the spec dir and emit
    deltas as they appear. This is the same file the portal's progress
    sidebar polls — it covers the 80% case (the user wants to *watch* an
    agent without needing the rmux pane).

    The richer rmux-driven SSE re-broadcast (which would let an MCP client
    drive a live terminal) is a follow-up — it depends on the rmux bridge
    being enabled, which isn't a given on all deployments. The poll-based
    fallback here works regardless.

    Client behaviour: subscribe to the stream, receive ``data:`` events,
    detect ``event: done`` when the agent finishes.
    """
    if ":" not in task_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid task ID format"
        )

    project_id, spec_id = task_id.split(":", 1)
    projects = load_projects()
    if project_id not in projects:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Project not found"
        )

    project_path = Path(projects[project_id]["path"])
    spec_dir = project_path / ".aifactory" / "specs" / spec_id
    if not spec_dir.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Task not found"
        )

    progress_file = spec_dir / "build-progress.txt"

    async def event_generator():
        """Yield SSE-formatted deltas from build-progress.txt.

        Sleeps 1s between polls. Emits an ``event: done`` line + closes
        when the file stops growing for 30s (heuristic: agent finished
        or the file isn't being written anymore). Caps total stream
        duration at 30 minutes to avoid leaking connections from
        misbehaving clients.
        """
        import asyncio

        max_duration_s = 30 * 60
        idle_timeout_s = 30
        poll_interval_s = 1.0
        start = asyncio.get_event_loop().time()
        last_size = 0
        last_change = start

        # Emit a kickoff event so the client knows the stream is live
        # even before there's content (useful when the agent hasn't
        # started writing yet).
        yield f"event: open\ndata: {json.dumps({'task_id': task_id, 'spec_id': spec_id})}\n\n"

        try:
            while True:
                now = asyncio.get_event_loop().time()
                if now - start > max_duration_s:
                    yield 'event: done\ndata: {"reason": "max-duration"}\n\n'
                    return

                if progress_file.exists():
                    current_size = progress_file.stat().st_size
                    if current_size > last_size:
                        with progress_file.open("rb") as fh:
                            fh.seek(last_size)
                            chunk = fh.read(current_size - last_size)
                        last_size = current_size
                        last_change = now
                        # SSE data lines: encode each newline as its own
                        # ``data:`` so multi-line chunks render correctly
                        # in standard EventSource clients.
                        text = chunk.decode("utf-8", errors="replace")
                        for line in text.splitlines():
                            yield f"data: {line}\n"
                        yield "\n"  # blank line terminates the event
                    elif now - last_change > idle_timeout_s:
                        yield 'event: done\ndata: {"reason": "idle-timeout"}\n\n'
                        return
                else:
                    # File doesn't exist yet — keep waiting, may appear
                    # once the agent starts writing.
                    if now - last_change > idle_timeout_s:
                        yield 'event: done\ndata: {"reason": "no-progress-file"}\n\n'
                        return

                await asyncio.sleep(poll_interval_s)
        except asyncio.CancelledError:
            # Client disconnected — fastapi cancels the generator.
            return

    from fastapi.responses import StreamingResponse

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.get("/{task_id}/plan-html")
async def get_plan_html(
    task_id: str, _access: dict = Depends(require_task_access("viewer"))
):
    """Generate and return HTML view of the implementation plan.

    Creates a temporary HTML file with nicely formatted plan for review.
    """
    if ":" not in task_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid task ID format",
        )

    project_id, spec_id = task_id.split(":", 1)
    projects = load_projects()

    if project_id not in projects:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found",
        )

    project_path = Path(projects[project_id]["path"])
    spec_dir = project_path / ".aifactory" / "specs" / spec_id

    if not spec_dir.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Task not found",
        )

    # Import HTML generator from backend
    import sys

    backend_path = Path(__file__).parent.parent.parent.parent / "backend"
    if str(backend_path) not in sys.path:
        sys.path.insert(0, str(backend_path))

    try:
        from review.html_generator import generate_html_plan_review

        # Generate HTML file
        html_file = generate_html_plan_review(spec_dir)

        # Return the HTML content
        from fastapi.responses import HTMLResponse

        return HTMLResponse(content=html_file.read_text(), status_code=200)

    except ImportError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"HTML generator not available: {str(e)}",
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to generate plan HTML: {str(e)}",
        )
