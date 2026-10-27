"""Stdio task proxies authorize the API key owner against project membership."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

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
projects_route = importlib.import_module("server.routes.projects")
tasks_route = importlib.import_module("server.routes.tasks")
execution_route = importlib.import_module("server.routes.execution")


def test_acw_principal_honors_scoped_service_token_flag(monkeypatch):
    monkeypatch.setattr(config.get_settings(), "SCOPED_SERVICE_TOKENS_ENABLED", True)
    key = AuthenticatedKey(
        key_id="key-owner",
        scopes=frozenset({"mcp:read"}),
        user_id="owner",
        org_id="org-a",
    )
    assert stdio_auth.acw_principal(key)["scopes"] == ["mcp:read"]
    assert stdio_auth.acw_principal(stdio_auth._LegacyAdminKey())["is_service"]


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
        ("/tasks/{task_id}/apply-correction", "execution", "apply_task_correction"),
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
            url,
            headers={"Authorization": f"Bearer {token}"},
            # apply-correction's body model requires fix_request_md.
            json={"fix_request_md": "fix"} if path.endswith("correction") else {},
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


@pytest.fixture
def read_app(monkeypatch):
    monkeypatch.setattr(project_authz, "_auth_disabled", lambda: False)
    projects = {
        "p1": {"org_id": "org-a", "path": "/unused"},
        "p2": {"org_id": "org-b", "path": "/unused"},
    }
    monkeypatch.setattr(project_registry, "load_projects", lambda: projects)
    monkeypatch.setattr(config.get_settings(), "API_TOKEN", "legacy-test-token")

    async def authenticate(header):
        user_id = {"Bearer acw_owner": "owner", "Bearer acw_service": None}.get(
            header, "foreign"
        )
        return AuthenticatedKey(
            key_id=f"key-{user_id}",
            scopes=frozenset({"mcp:read"}),
            user_id=user_id,
            org_id="org-a",
        )

    monkeypatch.setattr(stdio_auth.mcp_remote_auth, "authenticate", authenticate)

    class FakeResult:
        def __init__(self, user_id):
            self.user_id = user_id

        def scalar_one_or_none(self):
            return (
                OrgMember(org_id="org-a", user_id="owner", role="viewer")
                if self.user_id == "owner"
                else None
            )

        def scalars(self):
            return self

        def all(self):
            return ["org-a"] if self.user_id == "owner" else []

    class FakeSession(AsyncSession):
        async def execute(self, statement):
            params = statement.compile().params.values()
            user_id = "owner" if "owner" in params else "foreign"
            return FakeResult(user_id)

    async def fake_db():
        yield FakeSession()

    async def no_audit(*args, **kwargs):
        pass

    monkeypatch.setattr(project_authz, "_audit_authz_denial", no_audit)
    app = FastAPI()
    app.include_router(stdio_router.router)
    app.dependency_overrides[get_db] = fake_db
    return app


@pytest.mark.parametrize(
    ("path", "module", "handler"),
    [
        ("/tasks/{task_id}", "tasks", "get_task"),
        ("/tasks/{task_id}/status", "execution", "get_task_status"),
        ("/tasks/{task_id}/logs", "tasks", "get_task_logs"),
        ("/tasks/{task_id}/worktree/diff", "tasks", "get_worktree_diff"),
    ],
)
async def test_task_read_requires_project_membership(
    read_app, monkeypatch, path, module, handler
):
    calls = []

    async def delegated(*args, **kwargs):
        calls.append((args, kwargs))
        return {"ok": True}

    monkeypatch.setattr(
        importlib.import_module(f"server.routes.{module}"), handler, delegated
    )

    async with AsyncClient(
        transport=ASGITransport(app=read_app), base_url="http://test"
    ) as client:

        async def get(project_id, token):
            return await client.get(
                "/api/mcp-stdio" + path.format(task_id=f"{project_id}:001"),
                headers={"Authorization": f"Bearer {token}"},
            )

        assert (await get("p1", "acw_foreign")).status_code == 403
        assert calls == []
        assert (await get("p1", "acw_owner")).status_code == 200
        assert len(calls) == 1
        assert (await get("p9", "acw_owner")).status_code == 404
        assert len(calls) == 1
        assert (await get("p1", "legacy-test-token")).status_code == 200
        assert len(calls) == 2


@pytest.mark.parametrize("path", ["/projects", "/tasks", "/tasks/running"])
async def test_read_lists_filter_foreign_projects(read_app, monkeypatch, path):
    projects = {"p1": {"org_id": "org-a"}, "p2": {"org_id": "org-b"}}

    monkeypatch.setattr(stdio_router, "load_projects", lambda: projects)

    monkeypatch.setattr(
        projects_route, "project_to_response", lambda pid, _pdata: {"id": pid}
    )

    async def list_tasks(*, project_id, status, request, db):
        assert project_id is None and status is None
        allowed = await project_authz.accessible_org_ids(request, db)
        return {
            "tasks": [
                {"id": f"{pid}:001"}
                for pid, project in projects.items()
                if allowed is None or project["org_id"] in allowed
            ]
        }

    async def get_running_tasks():
        return execution_route.RunningTasksResponse(tasks=["p1:001", "p2:001"], count=2)

    monkeypatch.setattr(tasks_route, "list_tasks", list_tasks)
    monkeypatch.setattr(execution_route, "get_running_tasks", get_running_tasks)

    async with AsyncClient(
        transport=ASGITransport(app=read_app), base_url="http://test"
    ) as client:

        async def get(token):
            response = await client.get(
                "/api/mcp-stdio" + path,
                headers={"Authorization": f"Bearer {token}"},
            )
            assert response.status_code == 200
            return response.json()

        # An acw_ key with no user (user_id=None) is a service principal and
        # sees every project, matching REST (server.auth._acw_principal).
        for token, expected in [
            ("acw_foreign", []),
            ("acw_owner", ["p1"]),
            ("acw_service", ["p1", "p2"]),
            ("legacy-test-token", ["p1", "p2"]),
        ]:
            result = await get(token)
            if path == "/projects":
                ids = [item["id"] for item in result]
            elif path == "/tasks":
                ids = [item["id"] for item in result["tasks"]]
            else:
                ids = result["tasks"]
                assert result["count"] == len(ids)
            assert sorted(i.partition(":")[0] for i in ids) == expected


async def test_key_bound_to_its_org_even_if_owner_is_member_of_both(monkeypatch):
    """An org-a key whose owner is also in org-b cannot reach org-b projects (#1913)."""
    monkeypatch.setattr(project_authz, "_auth_disabled", lambda: False)
    projects = {"p1": {"org_id": "org-a"}, "p2": {"org_id": "org-b"}}
    monkeypatch.setattr(project_registry, "load_projects", lambda: projects)
    monkeypatch.setattr(stdio_router, "load_projects", lambda: projects)
    monkeypatch.setattr(
        projects_route, "project_to_response", lambda pid, _pdata: {"id": pid}
    )

    async def authenticate(header):
        return AuthenticatedKey(
            key_id="key-u",
            scopes=frozenset({"mcp:read", "task:write"}),
            user_id="u",
            org_id="org-a",
        )

    monkeypatch.setattr(stdio_auth.mcp_remote_auth, "authenticate", authenticate)

    class FakeResult:
        def scalar_one_or_none(self):
            return OrgMember(org_id="any", user_id="u", role="owner")

        def scalars(self):
            return self

        def all(self):
            return ["org-a", "org-b"]

    class FakeSession(AsyncSession):
        async def execute(self, statement):
            return FakeResult()

    async def fake_db():
        yield FakeSession()

    async def no_audit(*args, **kwargs):
        pass

    async def delegated(*args, **kwargs):
        return {"ok": True}

    monkeypatch.setattr(project_authz, "_audit_authz_denial", no_audit)
    monkeypatch.setattr(stdio_router, "_audit_mcp_write", no_audit)
    monkeypatch.setattr(execution_route, "start_task", delegated)
    monkeypatch.setattr(tasks_route, "get_task", delegated)
    app = FastAPI()
    app.include_router(stdio_router.router)
    app.dependency_overrides[get_db] = fake_db
    headers = {"Authorization": "Bearer acw_u"}

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        base = "/api/mcp-stdio"
        assert (
            await client.post(f"{base}/tasks/p2:001/start", headers=headers, json={})
        ).status_code == 403
        assert (
            await client.get(f"{base}/tasks/p2:001", headers=headers)
        ).status_code == 403
        assert (
            await client.post(f"{base}/tasks/p1:001/start", headers=headers, json={})
        ).status_code == 200
        listed = await client.get(f"{base}/projects", headers=headers)
        assert [i["id"] for i in listed.json()] == ["p1"]
