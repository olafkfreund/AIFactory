"""Extracted task-logs sub-router — #556 (god-file split).

Wiring check: the log endpoints stay mounted at the same URLs under /api/tasks
after extraction from routes/tasks.py (tasks.py mounts the sub-router via
router.include_router so the public paths are unchanged).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "web-server"))

from fastapi import FastAPI  # noqa: E402
from server.routes import tasks_logs  # noqa: E402
from server.routes.tasks import router as tasks_router  # noqa: E402


def test_logs_routes_registered_on_sub_router():
    app = FastAPI()
    app.include_router(tasks_logs.router, prefix="/api/tasks")
    have = {
        (r.path, m) for r in app.routes for m in getattr(r, "methods", set()) or set()
    }
    for path, method in (
        ("/api/tasks/{task_id}/logs", "GET"),
        ("/api/tasks/{task_id}/logs/watch", "POST"),
        ("/api/tasks/{task_id}/logs/unwatch", "POST"),
    ):
        assert (path, method) in have, f"missing {method} {path}"


def test_logs_routes_still_mounted_on_tasks_router():
    # tasks.py re-mounts the sub-router, so the paths must appear on tasks.router too.
    paths = {r.path for r in tasks_router.routes}
    assert "/{task_id}/logs" in paths
    assert "/{task_id}/logs/watch" in paths
