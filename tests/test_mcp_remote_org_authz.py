"""Remote MCP tools authorize the calling key's owner, not the service token (#1910).

Real SQLite DB with real ``OrgMember`` rows and the real
``authorize_project_for_user``: a key for org A cannot act on, read or list
org B's projects and tasks, and still works on its own org.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

_WEB_SERVER_DIR = Path(__file__).parent.parent / "apps" / "web-server"
if str(_WEB_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_WEB_SERVER_DIR))

from server.database.models import Base, Organization, OrgMember, User  # noqa: E402
from server.mcp_remote import tools  # noqa: E402
from server.mcp_remote.auth import AuthenticatedKey  # noqa: E402
from server.routes import project_authz  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

TASK_TOOLS = [
    ("aifactory.get_task", "mcp:read"),
    ("aifactory.get_worktree_diff", "mcp:read"),
    ("aifactory.get_qa_report", "mcp:read"),
    ("aifactory.tail_agent_console", "mcp:read"),
    ("aifactory.start_task", "mcp:write"),
    ("aifactory.stop_task", "mcp:write"),
    ("aifactory.approve_plan", "mcp:write"),
    ("aifactory.reject_plan", "mcp:write"),
    ("aifactory.recover_task", "mcp:write"),
    ("aifactory.merge_pr", "mcp:write"),
]

# The owner is a member of BOTH orgs; the key is bound to org-a (#1914).
KEY_A = AuthenticatedKey(
    key_id="key-a",
    scopes=frozenset({"mcp:read", "mcp:write"}),
    user_id="u",
    org_id="org-a",
)


@pytest.fixture
async def call(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    session_local = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with session_local() as s:
        s.add(User(id="u", email="u@x.test", password_hash="x"))
        await s.flush()
        for org in ("org-a", "org-b"):
            s.add(Organization(id=org, name=org, slug=org, owner_id="u"))
            await s.flush()
            s.add(OrgMember(org_id=org, user_id="u", role="member"))
        await s.commit()

    async def fake_db():
        async with session_local() as s:
            yield s

    async def no_audit(*args, **kwargs):
        pass

    monkeypatch.setattr(tools, "get_db", fake_db)
    monkeypatch.setattr(project_authz, "_auth_disabled", lambda: False)
    monkeypatch.setattr(project_authz, "_audit_authz_denial", no_audit)
    projects = {"pa": {"org_id": "org-a"}, "pb": {"org_id": "org-b"}}
    monkeypatch.setattr("server.project_registry.load_projects", lambda: projects)
    monkeypatch.setattr(tools, "load_projects", lambda: projects)
    proxied = AsyncMock(return_value=[{"id": "pa"}, {"id": "pb"}])
    monkeypatch.setattr(tools, "_call_internal", proxied)

    async def _call(tool, args):
        return await tools.dispatch_tool_call(tool, args, KEY_A), proxied

    yield _call
    await engine.dispose()


@pytest.mark.parametrize(("tool", "_scope"), TASK_TOOLS)
async def test_other_org_task_is_refused(call, tool, _scope):
    result, proxied = await call(tool, {"task_id": "pb:001"})
    assert result.get("isError") is True
    assert proxied.await_count == 0


@pytest.mark.parametrize(("tool", "_scope"), TASK_TOOLS)
async def test_own_org_task_still_works(call, tool, _scope):
    result, _ = await call(tool, {"task_id": "pa:001"})
    assert not result.get("isError"), result


async def test_list_tasks_other_org_refused_own_org_works(call):
    result, proxied = await call("aifactory.list_tasks", {"project_id": "pb"})
    assert result.get("isError") is True
    assert proxied.await_count == 0
    result, proxied = await call("aifactory.list_tasks", {"project_id": "pa"})
    assert not result.get("isError")
    assert proxied.await_args.args[1] == "/api/projects/pa/tasks"


async def test_list_projects_hides_other_org(call):
    result, _ = await call("aifactory.list_projects", {})
    assert '"pa"' in result["content"][0]["text"]
    assert '"pb"' not in result["content"][0]["text"]


@pytest.mark.parametrize("task_id", ["pa:1/../../pb:001", "..", "pa:1?x=1", 7])
async def test_unsafe_task_id_refused(call, task_id):
    result, proxied = await call("aifactory.stop_task", {"task_id": task_id})
    assert result.get("isError") is True
    assert proxied.await_count == 0


async def test_unknown_project_refused(call):
    result, proxied = await call("aifactory.start_task", {"task_id": "p9:001"})
    assert result.get("isError") is True
    assert proxied.await_count == 0
