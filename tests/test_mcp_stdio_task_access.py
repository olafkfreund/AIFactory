"""Task write proxies authorize the API key owner against the task's project."""

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

from server import config, project_registry
from server.database.engine import get_db
from server.database.models import OrgMember
from server.mcp_remote.auth import AuthenticatedKey
from server.mcp_stdio import auth as stdio_auth
from server.routes import project_authz

stdio_router = importlib.import_module("server.mcp_stdio.router")


@pytest.mark.parametrize(
    ("path", "module", "handler"),
    [
        ("/tasks/create-and-run", "execution", "create_and_run_task"),
        ("/tasks/{task_id}/start", "execution", "start_task"),
        ("/tasks/{task_id}/stop", "execution", "stop_task"),
        ("/tasks/{task_id}/recover", "execution", "recover_task"),
        ("/tasks/{task_id}/approve-plan", "tasks", "approve_plan"),
        ("/tasks/{task_id}/worktree/create-pr", "tasks", "create_pr_from_task"),
        ("/tasks/{task_id}/worktree/merge", "tasks", "merge_worktree"),
    ],
)
async def test_task_write_requires_project_membership(
    path, module, handler, monkeypatch
):
    monkeypatch.setattr(project_authz, "_auth_disabled", lambda: False)
    monkeypatch.setattr(
        project_registry, "load_projects", lambda: {"p1": {"org_id": "org-a"}}
    )
    monkeypatch.setattr(config.get_settings(), "API_TOKEN", "legacy-test-token")

    async def authenticate(header):
        user_id = "owner" if header == "Bearer acw_owner" else "foreign"
        return AuthenticatedKey(
            key_id=f"key-{user_id}",
            scopes=frozenset({"task:write", "task:merge"}),
            user_id=user_id,
            org_id="org-a",
        )

    monkeypatch.setattr(stdio_auth.mcp_remote_auth, "authenticate", authenticate)

    class FakeResult:
        def __init__(self, membership):
            self.membership = membership

        def scalar_one_or_none(self):
            return self.membership

    class FakeSession:
        async def execute(self, statement):
            membership = (
                OrgMember(org_id="org-a", user_id="owner", role="member")
                if "owner" in statement.compile().params.values()
                else None
            )
            return FakeResult(membership)

    async def fake_db():
        yield FakeSession()

    async def no_audit(*args, **kwargs):
        pass

    calls = []

    async def delegated(*args, **kwargs):
        calls.append((args, kwargs))
        return {"ok": True}

    monkeypatch.setattr(
        importlib.import_module(f"server.routes.{module}"), handler, delegated
    )
    monkeypatch.setattr(stdio_router, "_audit_mcp_write", no_audit)
    monkeypatch.setattr(project_authz, "_audit_authz_denial", no_audit)

    app = FastAPI()
    app.include_router(stdio_router.router)
    app.dependency_overrides[get_db] = fake_db

    async def post(client, project_id, token):
        url = "/api/mcp-stdio" + path.format(task_id=f"{project_id}:001")
        if path == "/tasks/create-and-run":
            url += f"?project_id={project_id}&title=title&description=description"
        return await client.post(
            url, headers={"Authorization": f"Bearer {token}"}, json={}
        )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await post(client, "p1", "acw_foreign")).status_code == 403
        assert calls == []

        assert (await post(client, "p1", "acw_owner")).status_code == 200
        assert len(calls) == 1

        assert (await post(client, "p9", "acw_owner")).status_code == 404
        assert len(calls) == 1

        assert (await post(client, "p1", "legacy-test-token")).status_code == 200
        assert len(calls) == 2
