"""Factory CLI plan rejection and archive use scoped stdio proxies."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

_WEB_SERVER = Path(__file__).parent.parent / "apps" / "web-server"
if str(_WEB_SERVER) not in sys.path:
    sys.path.insert(0, str(_WEB_SERVER))

from server import config, project_registry  # noqa: E402
from server.database.engine import get_db  # noqa: E402
from server.mcp_remote.auth import AuthenticatedKey  # noqa: E402
from server.mcp_stdio import auth as stdio_auth  # noqa: E402
from server.routes import project_authz  # noqa: E402

stdio_router = importlib.import_module("server.mcp_stdio.router")
projects_route = importlib.import_module("server.routes.projects")


@pytest.fixture
def app(tmp_path, monkeypatch):
    projects = {
        "p1": {"org_id": "org-a", "team_id": "team-a", "path": str(tmp_path)},
        "p2": {"org_id": "org-b", "team_id": "team-b", "path": str(tmp_path)},
        "p3": {"org_id": "org-a", "team_id": "team-c", "path": str(tmp_path)},
    }
    monkeypatch.setattr(project_authz, "_auth_disabled", lambda: False)
    monkeypatch.setattr(project_registry, "load_projects", lambda: projects)

    async def aload_projects():
        return projects

    monkeypatch.setattr(project_registry, "aload_projects", aload_projects)
    monkeypatch.setattr(config.get_settings(), "API_TOKEN", "unused-test-token")

    async def authenticate(header):
        return AuthenticatedKey(
            key_id="test-key",
            scopes=frozenset(
                {"mcp:read"} if header.endswith("read") else {"task:write"}
            ),
            user_id="owner",
            org_id="org-a",
        )

    monkeypatch.setattr(stdio_auth.mcp_remote_auth, "authenticate", authenticate)

    class Result:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    class Session:
        def in_transaction(self):
            return False

        async def rollback(self):
            pass

        async def execute(self, statement):
            sql = str(statement)
            other_team = "team-c" in statement.compile().params.values()
            if "teams.is_default" in sql:
                return Result(not other_team)
            return Result(None if other_team else "member")

    async def fake_db():
        yield Session()

    audits = []

    async def no_audit(*args, **kwargs):
        audits.append((args, kwargs))

    async def no_authz_audit(*args, **kwargs):
        pass

    monkeypatch.setattr(stdio_router, "_audit_mcp_write", no_audit)
    monkeypatch.setattr(project_authz, "_audit_authz_denial", no_authz_audit)
    api = FastAPI()
    api.include_router(stdio_router.router)
    api.dependency_overrides[get_db] = fake_db
    api.state.audits = audits
    return api


@pytest.mark.parametrize(
    ("path", "body", "expected"),
    [
        ("/tasks/p1:001/reject-plan", {"feedback": "Needs work"}, {"success": True}),
        (
            "/projects/p1/tasks/archive",
            {"taskIds": ["p1:001"]},
            {"success": True, "archivedCount": 1, "errors": None},
        ),
    ],
)
async def test_proxy_success_and_access(app, monkeypatch, path, body, expected):
    calls = []

    async def delegated(*args):
        calls.append(args)
        return expected

    if "reject-plan" in path:
        monkeypatch.setattr(stdio_router, "reject_plan", delegated)
    else:
        monkeypatch.setattr(projects_route, "archive_tasks", delegated)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:

        async def post(url, token="acw_write"):
            return await client.post(
                "/api/mcp-stdio" + url,
                headers={"Authorization": f"Bearer {token}"},
                json=body,
            )

        assert (await post(path)).json() == expected
        assert len(calls) == 1
        assert calls[0][0] == ("p1:001" if "reject-plan" in path else "p1")
        assert (
            calls[0][1].feedback == "Needs work"
            if "reject-plan" in path
            else calls[0][1].taskIds == ["p1:001"]
        )
        assert len(app.state.audits) == 1
        assert app.state.audits[0][0][1] == (
            "mcp.task.reject_plan" if "reject-plan" in path else "mcp.task.archive"
        )
        if "archive" in path:
            assert app.state.audits[0][1]["details"] == {"task_ids": ["p1:001"]}
        assert (await post(path, "acw_read")).status_code == 403
        assert (await post(path.replace("p1", "p2"))).status_code == 404
        assert (await post(path.replace("p1", "p3"))).status_code == 404
        assert (await post(path.replace("p1", "missing"))).status_code == 404
        assert len(calls) == 1
        assert len(app.state.audits) == 1


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/tasks/p1:missing/reject-plan", {"feedback": "Needs work"}),
        ("/projects/p1/tasks/archive", {"taskIds": ["p1:missing"]}),
    ],
)
async def test_proxy_unknown_task(app, path, body):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        result = await client.post(
            "/api/mcp-stdio" + path,
            headers={"Authorization": "Bearer acw_write"},
            json=body,
        )
    assert result.status_code == 404
    assert "not found" in result.json()["detail"].lower()


@pytest.mark.parametrize("task_ids", [["p2:001"], ["p1:001", "p2:002"], []])
async def test_archive_rejects_invalid_task_ids_without_writing(
    app, monkeypatch, tmp_path, task_ids
):
    plan = tmp_path / ".aifactory" / "specs" / "001" / "implementation_plan.json"
    plan.parent.mkdir(parents=True)
    plan.write_text('{"status": "done"}')
    calls = []

    async def delegated(*args):
        calls.append(args)
        return {"success": True}

    monkeypatch.setattr(projects_route, "archive_tasks", delegated)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        result = await client.post(
            "/api/mcp-stdio/projects/p1/tasks/archive",
            headers={"Authorization": "Bearer acw_write"},
            json={"taskIds": task_ids},
        )
    assert result.status_code == (422 if not task_ids else 404)
    assert calls == []
    assert app.state.audits == []
    assert plan.read_text() == '{"status": "done"}'


async def test_reject_plan_bad_body_returns_422(app, monkeypatch):
    calls = []

    async def delegated(*args):
        calls.append(args)

    monkeypatch.setattr(stdio_router, "reject_plan", delegated)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        result = await client.post(
            "/api/mcp-stdio/tasks/p1:001/reject-plan",
            headers={"Authorization": "Bearer acw_write"},
            json={"feedback": ["invalid"]},
        )
    assert result.status_code == 422
    assert calls == []
    assert app.state.audits == []
