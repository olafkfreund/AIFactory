"""End-to-end 503 tests when the build backend refuses a launch (#1943)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "apps" / "web-server"))
sys.path.insert(0, str(_ROOT / "apps" / "backend"))

from server.config import get_settings  # noqa: E402
from server.error_ref import BUILD_BACKEND_UNAVAILABLE  # noqa: E402
from server.main import create_app  # noqa: E402
from server.routes import execution as exec_mod  # noqa: E402
from server.routes import from_issue  # noqa: E402
from server.services import agent_service as agent_service_mod  # noqa: E402
from server.services import auto_fix_service  # noqa: E402
from server.services.build_backend import BuildBackendUnavailableError  # noqa: E402
from trusted_plan import APPROVAL_KEY, sign_plan  # noqa: E402

_KEY = "super-secret-cfactory-key"


class _RefusingAgentService:
    async def is_running_anywhere(self, _task_id: str) -> bool:
        return False

    async def start_spec_creation(self, **_kwargs):
        raise BuildBackendUnavailableError("Builds refused: secret-ish detail")

    async def start_task_execution(self, **_kwargs):
        raise BuildBackendUnavailableError("Builds refused: secret-ish detail")


class _StubContractStore:
    async def put(self, *_args, **_kwargs):
        return None


@pytest.fixture
def env(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    (project / ".aifactory" / "specs").mkdir(parents=True)
    monkeypatch.setattr(get_settings(), "DISABLE_AUTH", True)
    projects = {"p": {"path": str(project)}}
    monkeypatch.setattr(exec_mod, "load_projects", lambda: projects)
    monkeypatch.setattr(exec_mod, "resolve_project_id", lambda pid: pid)
    svc = _RefusingAgentService()
    monkeypatch.setattr(exec_mod, "get_agent_service", lambda: svc)
    monkeypatch.setattr(agent_service_mod, "get_agent_service", lambda: svc)
    return TestClient(create_app()), project


def _assert_503(resp) -> None:
    assert resp.status_code == 503
    assert resp.json() == {"detail": BUILD_BACKEND_UNAVAILABLE}
    assert "secret-ish" not in resp.text


def test_start_spec_creation_returns_503(env):
    client, project = env
    spec_dir = project / ".aifactory" / "specs" / "001-task"
    spec_dir.mkdir()
    (spec_dir / "requirements.json").write_text(
        json.dumps({"title": "T", "description": "D"})
    )
    _assert_503(client.post("/api/tasks/p:001-task/start", json={}))


def test_start_delegation_returns_503(env, monkeypatch):
    client, project = env
    spec_dir = project / ".aifactory" / "specs" / "001-task"
    spec_dir.mkdir()
    (spec_dir / "implementation_plan.json").write_text(json.dumps({"phases": []}))
    (spec_dir / "task_metadata.json").write_text(
        json.dumps({"enableDelegation": True, "githubIssueNumber": 7})
    )
    monkeypatch.setattr(auto_fix_service, "_provider_for", lambda _pid: None)
    _assert_503(client.post("/api/tasks/p:001-task/start", json={}))


def test_start_build_returns_503(env):
    client, project = env
    spec_dir = project / ".aifactory" / "specs" / "001-task"
    spec_dir.mkdir()
    (spec_dir / "implementation_plan.json").write_text(json.dumps({"phases": []}))
    _assert_503(client.post("/api/tasks/p:001-task/start", json={}))


def test_create_and_run_returns_503(env):
    client, _project = env
    _assert_503(
        client.post(
            "/api/tasks/create-and-run",
            params={"project_id": "p", "title": "Gateway", "description": "build it"},
            json={},
        )
    )


def test_from_plan_returns_503(env, monkeypatch):
    client, _project = env
    monkeypatch.setenv("AIFACTORY_TRUSTED_PLAN_KEY_CFACTORY", _KEY)
    monkeypatch.setattr(exec_mod, "TrustedContractStore", _StubContractStore)
    plan = {
        "feature": "Signing key rotation status endpoint",
        "workflow_type": "feature",
        "phases": [
            {
                "id": "p1",
                "name": "Endpoint",
                "parallel_safe": True,
                "subtasks": [
                    {
                        "id": "st1",
                        "description": "status endpoint",
                        "status": "pending",
                        "files_to_create": ["app/routers/status.py"],
                    }
                ],
            }
        ],
    }
    plan[APPROVAL_KEY] = sign_plan(
        plan,
        key=_KEY,
        approved_by="cfactory",
        approval_timestamp="2026-06-06T10:00:00Z",
    )
    _assert_503(
        client.post(
            "/api/tasks/from-plan",
            params={"project_id": "p", "title": "Contract", "description": "signed"},
            json={"plan": plan},
        )
    )


def test_from_issue_returns_503(env, monkeypatch):
    client, project = env

    async def _fetch(_pid, number, **_kwargs):
        return {"number": number, "title": "Issue", "body": "", "labels": []}

    monkeypatch.setattr(from_issue, "_resolve_project_path", lambda _pid: project)
    monkeypatch.setattr(from_issue, "_fetch_issue_via_provider", _fetch)
    _assert_503(
        client.post(
            "/api/tasks/from-issue", json={"project_id": "p", "issue_number": 7}
        )
    )
