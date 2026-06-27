"""Extracted task read-view sub-router — #556 (god-file split).

Wiring check: the read-view endpoints stay mounted at the same URLs under
/api/tasks after extraction (tasks.py re-mounts via router.include_router).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "web-server"))

from fastapi import FastAPI  # noqa: E402
from server.routes import tasks_views  # noqa: E402
from server.routes.tasks import router as tasks_router  # noqa: E402


def test_view_routes_registered_on_sub_router():
    app = FastAPI()
    app.include_router(tasks_views.router, prefix="/api/tasks")
    have = {
        (r.path, m) for r in app.routes for m in getattr(r, "methods", set()) or set()
    }
    for path in (
        "/api/tasks/{task_id}/qa-report",
        "/api/tasks/{task_id}/agent-console/sse",
        "/api/tasks/{task_id}/plan-html",
    ):
        assert (path, "GET") in have, f"missing {path}"


def test_view_routes_still_mounted_on_tasks_router():
    paths = {r.path for r in tasks_router.routes}
    assert "/{task_id}/qa-report" in paths
    assert "/{task_id}/plan-html" in paths
