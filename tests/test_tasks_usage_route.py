"""Extracted task-usage sub-router — #556 (god-file split).

Wiring check: the usage endpoints stay mounted at the same URLs under /api/tasks
after extraction (tasks.py re-mounts via router.include_router).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "web-server"))

from fastapi import FastAPI  # noqa: E402
from server.routes import tasks_usage  # noqa: E402
from server.routes.tasks import router as tasks_router  # noqa: E402


def test_usage_routes_registered_on_sub_router():
    app = FastAPI()
    app.include_router(tasks_usage.router, prefix="/api/tasks")
    have = {
        (r.path, m) for r in app.routes for m in getattr(r, "methods", set()) or set()
    }
    assert ("/api/tasks/{task_id}/token-usage", "GET") in have
    assert ("/api/tasks/{task_id}/resource-usage", "GET") in have


def test_usage_routes_still_mounted_on_tasks_router():
    paths = {r.path for r in tasks_router.routes}
    assert "/{task_id}/token-usage" in paths
    assert "/{task_id}/resource-usage" in paths


def test_token_usage_route_serves_the_cache_split(tmp_path, monkeypatch):
    """#1398: the route returns read_breakdown, which now carries the split."""
    import asyncio
    import json

    from server.routes import tasks as tasks_routes

    spec_dir = tmp_path / ".aifactory" / "specs" / "001"
    spec_dir.mkdir(parents=True)
    (spec_dir / "token_usage.json").write_text(
        json.dumps(
            {
                "version": 1,
                "turns": 1,
                "totalInputTokens": 1100,
                "categories": {},
                "cacheReadTokens": 900,
                "cacheCreationTokens": 100,
            }
        )
    )
    monkeypatch.setattr(
        tasks_routes, "_resolve_task", lambda _t: ("p", "001", tmp_path, spec_dir)
    )
    body = asyncio.run(tasks_usage.get_task_token_usage("p:001", _access={}))
    assert (body["cacheReadTokens"], body["cacheCreationTokens"]) == (900, 100)
    assert body["cacheHitRate"] == 0.9
