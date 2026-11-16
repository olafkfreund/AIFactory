"""Organization filtering and revocation on all-task WebSockets."""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, WebSocketDisconnect
from fastapi.testclient import TestClient

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "apps" / "web-server"))
sys.path.insert(0, str(_ROOT / "apps" / "backend"))

from server import auth  # noqa: E402
from server.websockets import logs, progress, task_access  # noqa: E402


@pytest.fixture
def sockets(monkeypatch):
    state = {"members": {"a"}, "error": False, "checked": []}

    class Session:
        async def __aenter__(self):
            if state["error"]:
                raise RuntimeError("database unavailable")
            return self

        async def __aexit__(self, *_):
            pass

    async def authorize(user, project_id, _db):
        state["checked"].append(project_id)
        if project_id not in state["members"] or (
            user.get("api_key_id") and user.get("org_id") != "A"
        ):
            raise PermissionError("not a member")

    class Service:
        def __init__(self):
            self.running = ["a:one", "a:two", "b:one", "bare"]
            self.callbacks = {"logs": {}, "progress": {}}

        def get_running_tasks(self):
            return list(self.running)

        def register_log_callback(self, task_id, callback):
            self.callbacks["logs"][task_id] = callback
            return lambda: self.callbacks["logs"].pop(task_id, None)

        def register_progress_callback(self, task_id, callback):
            self.callbacks["progress"][task_id] = callback
            return lambda: self.callbacks["progress"].pop(task_id, None)

    monkeypatch.setattr(task_access.engine, "async_session_factory", Session)
    monkeypatch.setattr(task_access, "authorize_project_for_user", authorize)
    monkeypatch.setattr(
        auth,
        "get_settings",
        lambda: SimpleNamespace(DISABLE_AUTH=False, API_TOKEN="legacy"),  # noqa: S106
    )
    monkeypatch.setattr(
        auth,
        "_try_decode_jwt",
        lambda token: {"sub": "owner"} if token == "jwt" else None,  # noqa: S105
    )
    validator = AsyncMock(
        return_value={"id": "owner", "org_id": "A", "api_key_id": "key"}
    )
    monkeypatch.setattr(auth, "_ws_acw_principal", validator)
    monkeypatch.setattr(task_access, "_ws_acw_principal", validator)
    service = Service()
    monkeypatch.setattr(logs, "get_agent_service", lambda: service)
    monkeypatch.setattr(progress, "get_agent_service", lambda: service)
    app = FastAPI()
    app.include_router(logs.router)
    app.include_router(progress.router)
    with TestClient(app) as client:
        yield client, service, state, validator


def connect(client, endpoint, token="jwt"):  # noqa: S107
    return client.websocket_connect(
        f"/ws/{endpoint}", headers={"authorization": f"Bearer {token}"}
    )


def item(endpoint, task_id):
    if endpoint == "logs":
        return SimpleNamespace(
            task_id=task_id,
            content="wake",
            timestamp="now",
            level="info",
            source="agent",
        )
    return SimpleNamespace(
        task_id=task_id,
        phase=SimpleNamespace(value="coding"),
        message="wake",
        timestamp="now",
        subtask=None,
        subtask_index=None,
        subtask_total=None,
        percentage=None,
        data={},
    )


def emit(client, service, endpoint, task_id):
    client.portal.call(service.callbacks[endpoint][task_id], item(endpoint, task_id))


def patch_clock(monkeypatch, endpoint):
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(
        logs if endpoint == "logs" else progress,
        "asyncio",
        SimpleNamespace(
            Queue=asyncio.Queue,
            wait_for=asyncio.wait_for,
            get_running_loop=lambda: SimpleNamespace(time=lambda: clock.now),
        ),
    )
    return clock


@pytest.mark.parametrize("endpoint", ["logs", "progress"])
def test_member_only_subscribes_to_own_project(sockets, endpoint):
    client, service, state, _validator = sockets
    with connect(client, endpoint) as ws:
        connected = ws.receive_json()
        assert connected["type"] == "connected"
        if endpoint == "progress":
            assert connected["running_tasks"] == ["a:one", "a:two"]
        assert set(service.callbacks[endpoint]) == {"a:one", "a:two"}
        assert state["checked"].count("a") == 1
        assert state["checked"].count("b") == 1
        emit(client, service, endpoint, "a:one")
        assert ws.receive_json()["task_id"] == "a:one"


@pytest.mark.parametrize("endpoint", ["logs", "progress"])
@pytest.mark.parametrize("token", ["legacy", "service"])
def test_trusted_principals_see_all(sockets, endpoint, token, monkeypatch):
    client, service, _state, _validator = sockets
    if token == "service":  # noqa: S105
        monkeypatch.setattr(
            task_access,
            "authenticate_websocket",
            AsyncMock(return_value={"id": "service", "is_service": True}),
        )
    with connect(client, endpoint, token) as ws:
        connected = ws.receive_json()
        assert set(service.callbacks[endpoint]) == set(service.running)
        if endpoint == "progress":
            assert connected["running_tasks"] == service.running


@pytest.mark.parametrize("endpoint", ["logs", "progress"])
def test_other_org_key_and_lookup_error_hide_tasks(sockets, endpoint, monkeypatch):
    client, service, state, _validator = sockets
    monkeypatch.setattr(
        auth,
        "_ws_acw_principal",
        AsyncMock(return_value={"id": "owner", "org_id": "B", "api_key_id": "key"}),
    )
    with connect(client, endpoint, "acw_key") as ws:
        connected = ws.receive_json()
        assert not service.callbacks[endpoint]
        if endpoint == "progress":
            assert connected["running_tasks"] == []
    state["error"] = True
    with connect(client, endpoint) as ws:
        connected = ws.receive_json()
        assert not service.callbacks[endpoint]
        if endpoint == "progress":
            assert connected["running_tasks"] == []


@pytest.mark.parametrize("endpoint", ["logs", "progress"])
@pytest.mark.parametrize("failure", ["none", "raises"])
def test_refresh_drops_queued_message_and_revoked_key(
    sockets, endpoint, failure, monkeypatch
):
    client, service, state, validator = sockets
    clock = patch_clock(monkeypatch, endpoint)
    monkeypatch.setattr(task_access, "_KEY_RECHECK_SECONDS", 0)
    with connect(client, endpoint, "acw_key") as ws:
        assert ws.receive_json()["type"] == "connected"
        emit(client, service, endpoint, "a:one")
        assert ws.receive_json()["task_id"] == "a:one"
        assert validator.await_count >= 2  # connect and a valid re-check
        clock.now = 4.9
        emit(client, service, endpoint, "a:one")
        assert ws.receive_json()["task_id"] == "a:one"
        state["members"].clear()
        clock.now = 5.0
        emit(client, service, endpoint, "a:one")
        # The queued item is denied at the refresh, then the empty stream heartbeats.
        heartbeat = ws.receive_json()
        assert heartbeat["type"] == "heartbeat"
        if endpoint == "progress":
            assert heartbeat["running_tasks"] == []
        assert not service.callbacks[endpoint]
        if failure == "none":
            validator.return_value = None
        else:
            validator.side_effect = RuntimeError("validator unavailable")
        clock.now = 10.0
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 4001


@pytest.mark.parametrize("endpoint", ["logs", "progress"])
@pytest.mark.parametrize("authorized", [True, False])
def test_final_event_after_task_leaves_running_list(
    sockets, endpoint, authorized, monkeypatch
):
    client, service, state, _validator = sockets
    clock = patch_clock(monkeypatch, endpoint)
    with connect(client, endpoint) as ws:
        assert ws.receive_json()["type"] == "connected"
        service.running.remove("a:one")
        if not authorized:
            state["members"].clear()
        clock.now = 5.0
        emit(client, service, endpoint, "a:one")
        message = ws.receive_json()
        assert message["type"] == (
            ("log" if endpoint == "logs" else "progress") if authorized else "heartbeat"
        )
        if authorized:
            assert message["task_id"] == "a:one"
        elif endpoint == "progress":
            assert message["running_tasks"] == []
        assert "a:one" not in service.callbacks[endpoint]


@pytest.mark.parametrize("endpoint", ["logs", "progress"])
def test_callbacks_stay_registered_during_slow_refresh(sockets, endpoint, monkeypatch):
    client, service, _state, _validator = sockets
    clock = patch_clock(monkeypatch, endpoint)
    entered = asyncio.Event()
    resume = asyncio.Event()
    slow = {"enabled": False}
    original = task_access._project_authorized

    async def authorize(user, project_id):
        if slow["enabled"] and project_id == "a":
            entered.set()
            await resume.wait()
        return await original(user, project_id)

    monkeypatch.setattr(task_access, "_project_authorized", authorize)
    with connect(client, endpoint) as ws:
        assert ws.receive_json()["type"] == "connected"
        slow["enabled"] = True
        clock.now = 5.0
        emit(client, service, endpoint, "a:one")
        client.portal.call(entered.wait)
        try:
            assert "a:one" in service.callbacks[endpoint]
            emit(client, service, endpoint, "a:one")
        finally:
            client.portal.call(resume.set)
        assert ws.receive_json()["task_id"] == "a:one"
        assert ws.receive_json()["task_id"] == "a:one"
