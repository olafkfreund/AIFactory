"""Authorization and revocation on per-task WebSocket streams."""

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
    state = {"members": {"owner"}, "error": None, "session_error": False}

    class Session:
        async def __aenter__(self):
            if state["session_error"]:
                raise RuntimeError("session unavailable")
            return self

        async def __aexit__(self, *_):
            pass

    async def authorize(user, project_id, _db):
        if state["error"]:
            raise RuntimeError(state["error"])
        if project_id != "project" or user["id"] not in state["members"]:
            raise PermissionError("not a member")
        if user.get("api_key_id") and user.get("org_id") != "A":
            raise PermissionError("wrong organization")

    class Service:
        log_callback = None
        progress_callback = None

        def register_log_callback(self, _task_id, _callback):
            self.log_callback = _callback
            return lambda: None

        def register_progress_callback(self, _task_id, _callback):
            self.progress_callback = _callback
            return lambda: None

        def is_running(self, _task_id):
            return False

    monkeypatch.setattr(task_access.engine, "async_session_factory", Session)
    monkeypatch.setattr(task_access, "authorize_project_for_user", authorize)
    service = Service()
    monkeypatch.setattr(logs, "get_agent_service", lambda: service)
    monkeypatch.setattr(progress, "get_agent_service", lambda: service)
    monkeypatch.setattr(
        auth,
        "get_settings",
        lambda: SimpleNamespace(
            DISABLE_AUTH=False, API_TOKEN="legacy", LEGACY_SHARED_TOKEN_FULL_ACCESS=True
        ),
    )
    monkeypatch.setattr(
        auth,
        "_try_decode_jwt",
        lambda token: {"sub": "owner"} if token == "jwt" else None,
    )
    principal = {"id": "owner", "org_id": "A", "api_key_id": "key"}
    validator = AsyncMock(return_value=principal)
    monkeypatch.setattr(auth, "_ws_acw_principal", validator)
    monkeypatch.setattr(task_access, "_ws_acw_principal", validator)
    app = FastAPI()
    app.include_router(logs.router)
    app.include_router(progress.router)
    with TestClient(app) as client:
        yield client, state, validator, service


def connect(client, endpoint, token="jwt", task_id="project:spec"):
    return client.websocket_connect(
        f"/ws/tasks/{task_id}/{endpoint}",
        headers={"authorization": f"Bearer {token}"},
    )


@pytest.mark.parametrize("endpoint", ["logs", "progress"])
@pytest.mark.parametrize("token", ["jwt", "acw_key", "legacy"])
def test_member_key_and_legacy_connect(sockets, endpoint, token):
    client, _state, _validator, _service = sockets
    with connect(client, endpoint, token) as ws:
        assert ws.receive_json()["type"] == "connected"


@pytest.mark.parametrize("endpoint", ["logs", "progress"])
def test_legacy_token_without_full_access_needs_project_grant(
    sockets, endpoint, monkeypatch
):
    client, _state, _validator, _service = sockets
    monkeypatch.setattr(
        task_access,
        "get_settings",
        lambda: SimpleNamespace(
            DISABLE_AUTH=False, LEGACY_SHARED_TOKEN_FULL_ACCESS=False
        ),
    )
    with pytest.raises(WebSocketDisconnect):
        with connect(client, endpoint, "legacy"):
            pass


@pytest.mark.parametrize("endpoint", ["logs", "progress"])
def test_auth_disabled_service_connects(sockets, endpoint, monkeypatch):
    client, _state, _validator, _service = sockets
    monkeypatch.setattr(
        auth,
        "get_settings",
        lambda: SimpleNamespace(
            DISABLE_AUTH=True, API_TOKEN="legacy", LEGACY_SHARED_TOKEN_FULL_ACCESS=True
        ),
    )
    with connect(client, endpoint, "service", "bare") as ws:
        assert ws.receive_json()["type"] == "connected"


@pytest.mark.parametrize("endpoint", ["logs", "progress"])
@pytest.mark.parametrize(
    ("task_id", "token", "error", "session_error"),
    [
        ("project", "jwt", None, False),
        ("project:", "jwt", None, False),
        (":spec", "jwt", None, False),
        ("missing:spec", "jwt", None, False),
        ("project:spec", "jwt", "registry unavailable", False),
        ("project:spec", "jwt", "query unavailable", False),
        ("project:spec", "jwt", None, True),
        ("project:spec", "acw_key", "query unavailable", False),
    ],
)
def test_connect_denied(sockets, endpoint, task_id, token, error, session_error):
    client, state, _validator, _service = sockets
    state["error"] = error
    state["session_error"] = session_error
    with pytest.raises(WebSocketDisconnect) as exc:
        with connect(client, endpoint, token, task_id):
            pass
    assert exc.value.code == 4403


@pytest.mark.parametrize("endpoint", ["logs", "progress"])
@pytest.mark.parametrize("token", ["jwt", "acw_key"])
def test_nonmember_denied(sockets, endpoint, token):
    client, state, _validator, _service = sockets
    state["members"].clear()
    with pytest.raises(WebSocketDisconnect) as exc:
        with connect(client, endpoint, token):
            pass
    assert exc.value.code == 4403


@pytest.mark.parametrize("endpoint", ["logs", "progress"])
def test_other_org_key_denied(sockets, endpoint, monkeypatch):
    client, _state, _validator, _service = sockets
    other = AsyncMock(return_value={"id": "owner", "org_id": "B", "api_key_id": "key"})
    monkeypatch.setattr(auth, "_ws_acw_principal", other)
    with pytest.raises(WebSocketDisconnect) as exc:
        with connect(client, endpoint, "acw_key"):
            pass
    assert exc.value.code == 4403


@pytest.mark.parametrize("endpoint", ["logs", "progress"])
@pytest.mark.parametrize(
    "failure", ["removed", "none", "raises", "registry", "query", "session"]
)
def test_key_recheck_closes(sockets, endpoint, failure, monkeypatch):
    client, state, validator, service = sockets
    with connect(client, endpoint, "acw_key") as ws:
        assert ws.receive_json()["type"] == "connected"
        if failure == "removed":
            state["members"].clear()
        elif failure == "none":
            validator.return_value = None
        elif failure == "raises":
            validator.side_effect = RuntimeError("validator unavailable")
        elif failure in {"registry", "query"}:
            state["error"] = f"{failure} unavailable"
        else:
            state["session_error"] = True
        monkeypatch.setattr(task_access, "_KEY_RECHECK_SECONDS", 0)
        if endpoint == "logs":
            callback = service.log_callback
            item = SimpleNamespace(
                task_id="project:spec",
                content="wake",
                timestamp="now",
                level="info",
                source="agent",
            )
        else:
            callback = service.progress_callback
            item = SimpleNamespace(
                task_id="project:spec",
                phase=SimpleNamespace(value="coding"),
                message="wake",
                timestamp="now",
                subtask=None,
                subtask_index=None,
                subtask_total=None,
                percentage=None,
                data={},
            )
        client.portal.call(callback, item)
        assert ws.receive_json()["type"] in {"log", "progress"}
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 4401
