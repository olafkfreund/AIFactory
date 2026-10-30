"""Tests for 503 response when the build backend refuses a launch (#1943)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "apps" / "web-server"))
sys.path.insert(0, str(_ROOT / "apps" / "backend"))

from server.error_ref import build_backend_unavailable  # noqa: E402
from server.main import create_app  # noqa: E402
from server.routes import execution as exec_mod  # noqa: E402
from server.routes.execution import CreateAndRunRequest  # noqa: E402
from server.services.build_backend import BuildBackendUnavailableError  # noqa: E402


class _RefusingAgentService:
    async def start_spec_creation(self, **kwargs):
        raise BuildBackendUnavailableError("Spec creation refused: x")


async def test_create_and_run_propagates_build_backend_unavailable(
    tmp_path, monkeypatch
):
    project = tmp_path / "proj"
    (project / ".aifactory" / "specs").mkdir(parents=True)
    monkeypatch.setattr(
        exec_mod, "load_projects", lambda: {"p": {"path": str(project)}}
    )
    monkeypatch.setattr(exec_mod, "get_agent_service", lambda: _RefusingAgentService())

    with pytest.raises(BuildBackendUnavailableError, match="Spec creation refused: x"):
        await exec_mod.create_and_run_task(
            project_id="p",
            title="Gateway",
            description="build it",
            request=CreateAndRunRequest(),
        )


def test_build_backend_unavailable_handler_returns_503():
    app = FastAPI()
    app.add_exception_handler(BuildBackendUnavailableError, build_backend_unavailable)

    @app.post("/launch")
    async def _launch():
        raise BuildBackendUnavailableError("Builds refused: x")

    resp = TestClient(app).post("/launch")
    assert resp.status_code == 503
    assert resp.json() == {"detail": "Builds refused: x"}


def test_create_app_registers_build_backend_unavailable_handler():
    app = create_app()
    assert (
        app.exception_handlers.get(BuildBackendUnavailableError)
        is build_backend_unavailable
    )
