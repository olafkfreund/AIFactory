#!/usr/bin/env python3
"""
Tests for the per-task agent resource-usage endpoint (#277).

Covers GET /api/tasks/{task_id}/resource-usage using a minimal FastAPI
TestClient that mounts only the tasks router. The route resolves a
``task_id`` (``projectId:specId``) to a live agent subprocess via the
AgentService singleton's ``running_tasks`` map and samples its CPU/RAM
with psutil.

Scenarios:
  - unknown task (bad format / missing project / missing spec) -> 404
  - valid task with no live process            -> not-running shape
  - valid task with a live (mocked) PID         -> populated shape
  - psutil error / dead PID mid-sample          -> degrades to not-running
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Add web-server to path so server modules are importable.
sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "web-server"))

from server.routes.tasks import router as tasks_router  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    """A project dir containing one valid spec (task) on disk."""
    spec_dir = tmp_path / ".aifactory" / "specs" / "001-demo"
    spec_dir.mkdir(parents=True)
    return tmp_path


@pytest.fixture
def _app() -> FastAPI:
    app = FastAPI(title="Tasks Resource-Usage Test App")
    app.include_router(tasks_router, prefix="/api/tasks")
    return app


@pytest.fixture
def client(_app: FastAPI, project_dir: Path):
    """TestClient with ``load_projects`` patched to expose one project.

    project_id = "proj1", path = project_dir. A valid task_id is
    therefore ``proj1:001-demo``.
    """
    projects = {"proj1": {"path": str(project_dir)}}
    with patch("server.routes.tasks.load_projects", return_value=projects):
        with TestClient(_app, raise_server_exceptions=True) as c:
            yield c


def _make_fake_proc(pid: int = 4242, returncode=None) -> MagicMock:
    """A stand-in for an asyncio subprocess held in running_tasks."""
    proc = MagicMock()
    proc.pid = pid
    proc.returncode = returncode
    return proc


# ---------------------------------------------------------------------------
# 404 — unknown task
# ---------------------------------------------------------------------------


class TestUnknownTask:
    def test_missing_project_returns_404(self, client):
        resp = client.get("/api/tasks/nope:001-demo/resource-usage")
        assert resp.status_code == 404

    def test_missing_spec_returns_404(self, client):
        resp = client.get("/api/tasks/proj1:does-not-exist/resource-usage")
        assert resp.status_code == 404

    def test_bad_format_returns_400(self, client):
        # No ':' separator -> invalid task_id format.
        resp = client.get("/api/tasks/badformat/resource-usage")
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Not-running shape
# ---------------------------------------------------------------------------


class TestNotRunning:
    def test_no_live_process_returns_not_running(self, client):
        svc = MagicMock()
        svc.running_tasks = {}  # nothing running
        with patch(
            "server.services.agent_service.get_agent_service",
            return_value=svc,
        ):
            resp = client.get("/api/tasks/proj1:001-demo/resource-usage")

        assert resp.status_code == 200
        body = resp.json()
        assert body["running"] is False
        assert body["pid"] is None
        assert body["cpuPercent"] == 0
        assert body["memoryMb"] == 0
        assert body["memoryPercent"] == 0
        assert isinstance(body["sampledAt"], str) and body["sampledAt"]

    def test_already_exited_process_returns_not_running(self, client):
        # Process object lingers but has a returncode -> treat as not running.
        svc = MagicMock()
        svc.running_tasks = {"proj1:001-demo": _make_fake_proc(returncode=0)}
        with patch(
            "server.services.agent_service.get_agent_service",
            return_value=svc,
        ):
            resp = client.get("/api/tasks/proj1:001-demo/resource-usage")

        assert resp.status_code == 200
        assert resp.json()["running"] is False


# ---------------------------------------------------------------------------
# Populated shape (mocked psutil)
# ---------------------------------------------------------------------------


class TestRunning:
    def test_live_pid_returns_populated_shape(self, client):
        svc = MagicMock()
        svc.running_tasks = {"proj1:001-demo": _make_fake_proc(pid=4242)}

        # Fake psutil.Process: parent + one child.
        parent = MagicMock()
        parent.pid = 4242
        parent.cpu_percent.return_value = 12.5
        parent.memory_info.return_value = MagicMock(rss=100 * 1024 * 1024)  # 100 MB

        child = MagicMock()
        child.pid = 4300
        child.cpu_percent.return_value = 7.5
        child.memory_info.return_value = MagicMock(rss=50 * 1024 * 1024)  # 50 MB
        parent.children.return_value = [child]

        fake_psutil = MagicMock()
        fake_psutil.Process.return_value = parent
        fake_psutil.virtual_memory.return_value = MagicMock(
            total=1000 * 1024 * 1024
        )  # 1000 MB
        # Real exception classes so the route's except clauses behave.
        fake_psutil.NoSuchProcess = type("NoSuchProcess", (Exception,), {})
        fake_psutil.AccessDenied = type("AccessDenied", (Exception,), {})
        fake_psutil.ZombieProcess = type("ZombieProcess", (Exception,), {})

        with (
            patch(
                "server.services.agent_service.get_agent_service",
                return_value=svc,
            ),
            patch.dict("sys.modules", {"psutil": fake_psutil}),
        ):
            resp = client.get("/api/tasks/proj1:001-demo/resource-usage")

        assert resp.status_code == 200
        body = resp.json()
        assert body["running"] is True
        assert body["pid"] == 4242
        # 12.5 (parent) + 7.5 (child) = 20.0
        assert body["cpuPercent"] == 20.0
        # 100 + 50 = 150 MB
        assert body["memoryMb"] == 150.0
        # 150 / 1000 * 100 = 15.0%
        assert body["memoryPercent"] == 15.0
        assert isinstance(body["sampledAt"], str) and body["sampledAt"]


# ---------------------------------------------------------------------------
# Defensive degradation
# ---------------------------------------------------------------------------


class TestDefensiveSampling:
    def test_dead_pid_degrades_to_not_running(self, client):
        svc = MagicMock()
        svc.running_tasks = {"proj1:001-demo": _make_fake_proc(pid=999999)}

        no_such = type("NoSuchProcess", (Exception,), {})
        fake_psutil = MagicMock()
        fake_psutil.NoSuchProcess = no_such
        fake_psutil.AccessDenied = type("AccessDenied", (Exception,), {})
        fake_psutil.ZombieProcess = type("ZombieProcess", (Exception,), {})
        # Constructing Process for a dead pid raises NoSuchProcess.
        fake_psutil.Process.side_effect = no_such()

        with (
            patch(
                "server.services.agent_service.get_agent_service",
                return_value=svc,
            ),
            patch.dict("sys.modules", {"psutil": fake_psutil}),
        ):
            resp = client.get("/api/tasks/proj1:001-demo/resource-usage")

        assert resp.status_code == 200
        body = resp.json()
        assert body["running"] is False
        assert body["pid"] is None
        assert body["cpuPercent"] == 0
        assert body["memoryMb"] == 0

    def test_unexpected_psutil_error_degrades_to_not_running(self, client):
        svc = MagicMock()
        svc.running_tasks = {"proj1:001-demo": _make_fake_proc(pid=4242)}

        fake_psutil = MagicMock()
        fake_psutil.NoSuchProcess = type("NoSuchProcess", (Exception,), {})
        fake_psutil.AccessDenied = type("AccessDenied", (Exception,), {})
        fake_psutil.ZombieProcess = type("ZombieProcess", (Exception,), {})
        # Some unexpected OS error during sampling.
        fake_psutil.Process.side_effect = RuntimeError("boom")

        with (
            patch(
                "server.services.agent_service.get_agent_service",
                return_value=svc,
            ),
            patch.dict("sys.modules", {"psutil": fake_psutil}),
        ):
            resp = client.get("/api/tasks/proj1:001-demo/resource-usage")

        assert resp.status_code == 200
        assert resp.json()["running"] is False
