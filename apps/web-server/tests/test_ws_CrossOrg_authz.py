"""Cross-org, cross-team, and scope-ceiling WebSocket authorization tests (#2181)."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, WebSocketDisconnect
from fastapi.testclient import TestClient
from server import project_registry
from server.database.models import (
    Base,
    Organization,
    OrgMember,
    Team,
    TeamMembership,
    User,
)
from server.pty import access as pty_access
from server.pty.manager import PTYManager
from server.pty.session import PTYSession
from server.routes import project_authz
from server.websockets import (
    event_bus,
    events,
    logs,
    progress,
    task_access,
    terminal as terminal_ws,
)
from server.websockets.event_bus import OrgScope
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

_DUMMY_HASH = "x"
_WEB_SERVER = Path(__file__).resolve().parents[1]
if str(_WEB_SERVER) not in sys.path:
    sys.path.insert(0, str(_WEB_SERVER))


@pytest.fixture
def seeded_ws_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Real SQLite DB wired into task_access, event_bus, and pty.access."""
    monkeypatch.setattr(project_authz, "_auth_disabled", lambda: False)
    monkeypatch.setattr(project_authz, "_audit_authz_denial", AsyncMock())

    projects = {
        "p1": {"org_id": "org-1", "team_id": "team-1", "path": str(tmp_path / "p1")},
        "p_team2": {
            "org_id": "org-1",
            "team_id": "team-2",
            "path": str(tmp_path / "p_team2"),
        },
        "p2": {"org_id": "org-2", "team_id": "team-org2", "path": str(tmp_path / "p2")},
    }
    monkeypatch.setattr(project_registry, "load_projects", lambda: projects)
    monkeypatch.setattr(
        project_registry, "aload_projects", AsyncMock(return_value=projects)
    )
    monkeypatch.setattr(events, "load_projects", lambda: projects)

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'ws_authz.db'}")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _seed() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with session_factory() as db:
            db.add_all(
                [
                    User(id="alice", email="a@x.test", password_hash=_DUMMY_HASH),
                    User(id="viewer-u", email="v@x.test", password_hash=_DUMMY_HASH),
                    User(id="bob", email="b@x.test", password_hash=_DUMMY_HASH),
                    Organization(
                        id="org-1", name="Org 1", slug="org-1", owner_id="alice"
                    ),
                    Organization(
                        id="org-2", name="Org 2", slug="org-2", owner_id="bob"
                    ),
                    Team(
                        id="team-1",
                        org_id="org-1",
                        name="Default",
                        slug="default",
                        is_default=True,
                    ),
                    Team(
                        id="team-2",
                        org_id="org-1",
                        name="Other",
                        slug="other",
                        is_default=False,
                    ),
                    Team(
                        id="team-org2",
                        org_id="org-2",
                        name="Default",
                        slug="default",
                        is_default=True,
                    ),
                    OrgMember(org_id="org-1", user_id="alice", role="member"),
                    OrgMember(org_id="org-1", user_id="viewer-u", role="viewer"),
                    OrgMember(org_id="org-2", user_id="bob", role="owner"),
                    TeamMembership(team_id="team-1", user_id="alice", role="member"),
                    TeamMembership(team_id="team-1", user_id="viewer-u", role="viewer"),
                    TeamMembership(team_id="team-org2", user_id="bob", role="owner"),
                ]
            )
            await db.commit()

    asyncio.run(_seed())
    monkeypatch.setattr(task_access.engine, "async_session_factory", session_factory)
    monkeypatch.setattr(pty_access.engine, "async_session_factory", session_factory)
    try:
        yield session_factory
    finally:
        asyncio.run(engine.dispose())


@pytest.mark.asyncio
@pytest.mark.usefixtures("seeded_ws_env")
async def test_ws_task_authorized_checks_team_grant_and_scope_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Alice is member of team-1 (p1), not team-2 (p_team2), not org-2 (p2).
    assert await task_access._project_authorized({"id": "alice"}, "p1") is True
    assert await task_access._project_authorized({"id": "alice"}, "p_team2") is False
    assert await task_access._project_authorized({"id": "alice"}, "p2") is False
    assert await task_access._project_authorized({"id": "alice"}, "unknown") is False

    # Token with unrecognized scopes is denied even for a team member.
    assert (
        await task_access._project_authorized(
            {"id": "alice", "scopes": ["deploy:read"]}, "p1"
        )
        is False
    )

    # Ownerless service key for org-1 reaches default-team p1, but not p_team2 or p2.
    svc_key = {
        "id": None,
        "org_id": "org-1",
        "scoped_service": True,
        "is_service": False,
        "scopes": ["mcp:read"],
    }
    assert await task_access._project_authorized(svc_key, "p1") is True
    assert await task_access._project_authorized(svc_key, "p_team2") is False
    assert await task_access._project_authorized(svc_key, "p2") is False

    # Legacy shared token respects LEGACY_SHARED_TOKEN_FULL_ACCESS.
    legacy = {
        "id": "default",
        "role": "user",
        "is_service": False,
        "is_legacy_shared_token": True,
    }
    monkeypatch.setattr(
        task_access,
        "get_settings",
        lambda: SimpleNamespace(
            DISABLE_AUTH=False, LEGACY_SHARED_TOKEN_FULL_ACCESS=False
        ),
    )
    monkeypatch.setattr(
        project_authz,
        "get_settings",
        lambda: SimpleNamespace(
            DISABLE_AUTH=False, LEGACY_SHARED_TOKEN_FULL_ACCESS=False
        ),
    )
    assert await task_access._project_authorized(legacy, "p1") is False


@pytest.mark.asyncio
@pytest.mark.usefixtures("seeded_ws_env")
async def test_event_bus_filters_cross_team_and_cross_org_project_events(
) -> None:
    class FakeSocket:
        def __init__(self) -> None:
            self.sent: list[dict[str, Any]] = []

        async def send_text(self, text: str) -> None:
            self.sent.append(json.loads(text))

    ws_alice = FakeSocket()
    ws_bob = FakeSocket()
    client_alice = event_bus.register_client(
        ws_alice, {"id": "alice", "org_id": "org-1"}
    )
    client_alice.org_ids = {"org-1"}
    client_bob = event_bus.register_client(ws_bob, {"id": "bob", "org_id": "org-2"})
    client_bob.org_ids = {"org-2"}

    try:
        await event_bus.deliver_local(
            OrgScope("org-1"), "task:update", {"projectId": "p1"}
        )
        await event_bus.deliver_local(
            OrgScope("org-1"), "task:update", {"projectId": "p_team2"}
        )
        await event_bus.deliver_local(
            OrgScope("org-2"), "task:update", {"projectId": "p2"}
        )

        # Alice receives p1 (team-1), NOT p_team2 (team-2 in org-1) and NOT p2 (org-2).
        assert [m["payload"]["projectId"] for m in ws_alice.sent] == ["p1"]
        # Bob receives p2 (org-2), NOT p1 or p_team2.
        assert [m["payload"]["projectId"] for m in ws_bob.sent] == ["p2"]
    finally:
        event_bus.unregister_client(ws_alice)
        event_bus.unregister_client(ws_bob)


@pytest.mark.usefixtures("seeded_ws_env")
def test_task_ws_and_terminal_ws_enforce_project_roles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principals = {
        "alice": {"id": "alice", "role": "user"},
        "viewer-u": {"id": "viewer-u", "role": "user"},
        "alice-read-key": {
            "id": "alice",
            "org_id": "org-1",
            "api_key_id": "k-read",
            "scopes": ["mcp:read"],
        },
        "bob": {"id": "bob", "role": "user"},
        "legacy": {
            "id": "default",
            "role": "user",
            "is_service": False,
            "is_legacy_shared_token": True,
        },
    }

    class DummyAgentService:
        def register_log_callback(self, _task_id, _cb):
            return lambda: None

        def register_progress_callback(self, _task_id, _cb):
            return lambda: None

        def is_running(self, _task_id):
            return False

        def get_running_tasks(self):
            return ["p1:001", "p_team2:002", "p2:003"]

    svc = DummyAgentService()
    monkeypatch.setattr(logs, "get_agent_service", lambda: svc)
    monkeypatch.setattr(progress, "get_agent_service", lambda: svc)

    async def fake_auth_ws(ws):
        token = ws.headers.get("authorization", "").removeprefix("Bearer ")
        return principals[token]

    monkeypatch.setattr(task_access, "authenticate_websocket", fake_auth_ws)
    monkeypatch.setattr(terminal_ws, "authenticate_websocket", fake_auth_ws)
    monkeypatch.setattr(
        PTYManager, "_resolve_claude_token", lambda _self: (None, None, None)
    )
    monkeypatch.setattr(PTYSession, "start", lambda _self: None)
    monkeypatch.setattr(PTYSession, "is_alive", lambda _self: True)
    monkeypatch.setattr(PTYSession, "close", lambda _self: None)
    manager = PTYManager()
    manager.create_session(session_id="t-alice", owner_id="alice", project_id="p1")
    manager.create_session(session_id="t-viewer", owner_id="viewer-u", project_id="p1")
    monkeypatch.setattr(terminal_ws, "get_pty_manager", lambda: manager)

    app = FastAPI()
    app.include_router(logs.router)
    app.include_router(progress.router)
    app.include_router(terminal_ws.router)

    with TestClient(app) as client:
        # Alice (member of p1) connects to p1 task logs, but is rejected (4403) on p_team2 and p2.
        with client.websocket_connect(
            "/ws/tasks/p1:001/logs", headers={"authorization": "Bearer alice"}
        ) as ws:
            assert ws.receive_json()["type"] == "connected"

        for forbidden_task in ("p_team2:002", "p2:003"):
            with (
                pytest.raises(WebSocketDisconnect) as exc,
                client.websocket_connect(
                    f"/ws/tasks/{forbidden_task}/logs",
                    headers={"authorization": "Bearer alice"},
                ),
            ):
                pass
            assert exc.value.code == 4403

        # Unified progress stream filters running_tasks to visible projects only.
        with client.websocket_connect(
            "/ws/progress", headers={"authorization": "Bearer alice"}
        ) as ws:
            msg = ws.receive_json()
            assert msg["running_tasks"] == ["p1:001"]

        # Terminal WS requires 'member' effective role: viewer-u or read-only key gets 4404.
        for bad_token, term_id in (
            ("viewer-u", "t-viewer"),
            ("alice-read-key", "t-alice"),
            ("bob", "t-alice"),
        ):
            with client.websocket_connect(
                f"/ws/terminal/{term_id}",
                headers={"authorization": f"Bearer {bad_token}"},
            ) as ws:
                assert ws.receive_json()["type"] == "error"
                with pytest.raises(WebSocketDisconnect) as exc:
                    ws.receive_json()
                assert exc.value.code == 4404

        # Disable LEGACY_SHARED_TOKEN_FULL_ACCESS -> legacy token denied on task WS.
        monkeypatch.setattr(
            task_access,
            "get_settings",
            lambda: SimpleNamespace(
                DISABLE_AUTH=False, LEGACY_SHARED_TOKEN_FULL_ACCESS=False
            ),
        )
        monkeypatch.setattr(
            project_authz,
            "get_settings",
            lambda: SimpleNamespace(
                DISABLE_AUTH=False, LEGACY_SHARED_TOKEN_FULL_ACCESS=False
            ),
        )
        with (
            pytest.raises(WebSocketDisconnect) as exc,
            client.websocket_connect(
                "/ws/tasks/p1:001/logs", headers={"authorization": "Bearer legacy"}
            ),
        ):
            pass
        assert exc.value.code == 4403
