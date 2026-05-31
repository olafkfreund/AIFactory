#!/usr/bin/env python3
"""
Regression tests for GitHub issue #232.

Two distinct sub-bugs were reported for the ``POST /api/tasks/create-and-run``
flow:

(a) **Doubled-slug "Stuck" card.** ``create-and-run`` used to run the spec
    pipeline under a temporary ``pending-<uuid>`` spec dir. The orchestrator
    later renamed that dir from the gathered requirements, which desynced the
    board task from the on-disk spec dir and produced a doubled slug
    (e.g. ``metrics-endpoint-metrics-endpoint``) that stranded as
    "Stuck / Interrupted". The fix assigns a stable ``NNN-slug`` id up front
    and seeds ``requirements.json`` so no rename ever happens.

(b) **``/api/tasks/running`` shadowed by the catch-all.** ``tasks.router`` has a
    catch-all ``GET /{task_id}``. If it is mounted before ``execution.router``
    (which owns ``GET /running``) at the shared ``/api/tasks`` prefix, then
    ``/api/tasks/running`` matches ``/{task_id}`` with ``task_id="running"`` and
    returns ``400 Invalid task ID format``. The fix mounts ``execution.router``
    first so the explicit ``/running`` route wins.

These tests lock in both fixes against regression.
"""

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Add web-server to path so server modules are importable
sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "web-server"))

from server.routes import execution as execution_routes  # noqa: E402
from server.routes import tasks as tasks_routes  # noqa: E402

# ---------------------------------------------------------------------------
# Bug (b): /api/tasks/running must not be shadowed by the catch-all route
# ---------------------------------------------------------------------------


def _build_app(execution_first: bool) -> FastAPI:
    """Mount the two routers that share the /api/tasks prefix.

    ``execution_first=True`` mirrors the production order in main.py.
    """
    app = FastAPI()
    if execution_first:
        app.include_router(execution_routes.router, prefix="/api/tasks")
        app.include_router(tasks_routes.router, prefix="/api/tasks")
    else:
        app.include_router(tasks_routes.router, prefix="/api/tasks")
        app.include_router(execution_routes.router, prefix="/api/tasks")
    return app


def test_running_route_not_shadowed_with_production_order():
    """GET /api/tasks/running resolves to the running-tasks handler.

    With execution.router mounted first (production order), the explicit
    /running route must win over the catch-all GET /{task_id}.
    """
    app = _build_app(execution_first=True)

    fake_service = MagicMock()
    fake_service.get_running_tasks.return_value = []

    with patch.object(execution_routes, "get_agent_service", return_value=fake_service):
        client = TestClient(app)
        resp = client.get("/api/tasks/running")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Shape of RunningTasksResponse — proves get_running_tasks() handled it,
    # NOT the catch-all get_task() which would 400 with "Invalid task ID format".
    assert body == {"tasks": [], "count": 0}


def test_wrong_router_order_demonstrates_the_shadowing_bug():
    """Documents why router order is load-bearing.

    If tasks.router (catch-all GET /{task_id}) is mounted first, then
    /api/tasks/running is swallowed and returns 400 — the exact symptom from
    issue #232. This test guards the *reason* for the ordering in main.py.
    """
    app = _build_app(execution_first=False)

    with patch.object(tasks_routes, "load_projects", return_value={}):
        client = TestClient(app)
        resp = client.get("/api/tasks/running")

    assert resp.status_code == 400
    assert "Invalid task ID format" in resp.text


def test_main_app_mounts_execution_before_tasks():
    """The production app must keep execution.router ahead of tasks.router."""
    from server import main

    src = Path(main.__file__).read_text()
    exec_idx = src.index("include_router(execution.router")
    tasks_idx = src.index("include_router(tasks.router")
    assert exec_idx < tasks_idx, (
        "execution.router must be included before tasks.router so the explicit "
        "/api/tasks/running route is not shadowed by the catch-all GET /{task_id}"
    )


# ---------------------------------------------------------------------------
# Bug (a): create-and-run assigns a stable NNN-slug id (no pending/doubling)
# ---------------------------------------------------------------------------


@pytest.fixture
def project(tmp_path):
    """A registered project backed by a fresh temp dir (empty specs counter)."""
    project_path = tmp_path / "proj"
    (project_path / ".aifactory" / "specs").mkdir(parents=True)
    return "p1", project_path


def test_create_and_run_assigns_stable_numbered_spec_id(project):
    project_id, project_path = project

    fake_service = MagicMock()
    fake_service.start_spec_creation = AsyncMock(return_value=None)

    app = FastAPI()
    app.include_router(execution_routes.router, prefix="/api/tasks")

    with patch.object(
        execution_routes,
        "load_projects",
        return_value={project_id: {"path": str(project_path)}},
    ), patch.object(
        execution_routes, "get_agent_service", return_value=fake_service
    ):
        client = TestClient(app)
        resp = client.post(
            "/api/tasks/create-and-run",
            params={
                "project_id": project_id,
                "title": "Add /metrics endpoint",
                "description": "Expose Prometheus metrics at /metrics",
            },
            json={},
        )

    assert resp.status_code == 200, resp.text
    task_id = resp.json()["task_id"]
    assert task_id.startswith(f"{project_id}:")
    spec_id = task_id.split(":", 1)[1]

    # Fresh project -> deterministic first id. A numbered, board-trackable slug
    # with NO "pending-" prefix and NO doubled slug.
    assert spec_id == "001-add-metrics-endpoint"
    assert not spec_id.startswith("pending-")

    # The spec dir the board tracks must exist and be seeded with requirements,
    # so the finished build surfaces at the review gate (not as "Stuck").
    spec_dir = project_path / ".aifactory" / "specs" / spec_id
    assert spec_dir.is_dir()
    reqs = json.loads((spec_dir / "requirements.json").read_text())
    assert reqs["title"] == "Add /metrics endpoint"
    assert reqs["description"] == "Expose Prometheus metrics at /metrics"

    # The pipeline must be driven with the SAME id the board tracks (in sync).
    fake_service.start_spec_creation.assert_awaited_once()
    assert fake_service.start_spec_creation.await_args.kwargs["task_id"] == task_id


def test_create_and_run_slug_is_not_doubled(project):
    """Even when title words repeat, the slug must not be doubled."""
    project_id, project_path = project

    fake_service = MagicMock()
    fake_service.start_spec_creation = AsyncMock(return_value=None)

    app = FastAPI()
    app.include_router(execution_routes.router, prefix="/api/tasks")

    with patch.object(
        execution_routes,
        "load_projects",
        return_value={project_id: {"path": str(project_path)}},
    ), patch.object(
        execution_routes, "get_agent_service", return_value=fake_service
    ):
        client = TestClient(app)
        resp = client.post(
            "/api/tasks/create-and-run",
            params={
                "project_id": project_id,
                "title": "metrics endpoint",
                "description": "metrics endpoint",
            },
            json={},
        )

    assert resp.status_code == 200, resp.text
    spec_id = resp.json()["task_id"].split(":", 1)[1]
    assert spec_id == "001-metrics-endpoint"
    # Guard against the reported doubled slug (metrics-endpoint-metrics-endpoint).
    slug = spec_id.split("-", 1)[1]
    assert slug != "metrics-endpoint-metrics-endpoint"
