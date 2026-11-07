"""Scoped API keys on the shared WebSocket auth path and event endpoint."""

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import WebSocketDisconnect

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "apps" / "web-server"))
sys.path.insert(0, str(_ROOT / "apps" / "backend"))

from server import auth as auth_mod  # noqa: E402
from server.database import engine  # noqa: E402
from server.mcp_remote import auth as mcp_auth  # noqa: E402
from server.websockets import event_bus, events  # noqa: E402
from server.websockets.event_bus import OrgScope  # noqa: E402


class FakeWebSocket:
    headers = {}
    query_params = {"token": "acw_test"}

    def __init__(self):
        self.closed = []

    async def close(self, **kwargs):
        self.closed.append(kwargs)


@pytest.fixture
def ws_auth(monkeypatch):
    monkeypatch.setattr(
        auth_mod,
        "get_settings",
        lambda: SimpleNamespace(DISABLE_AUTH=False, API_TOKEN="legacy"),
    )
    monkeypatch.setattr(auth_mod, "_try_decode_jwt", lambda token: None)
    monkeypatch.setattr(auth_mod, "_warn_ws_query_token_once", lambda: None)


def key(*, scopes=frozenset({"api"}), user_id="user", org_id="A"):
    return mcp_auth.AuthenticatedKey("key-id", scopes, user_id, org_id)


@pytest.mark.asyncio
async def test_owned_key_has_rest_principal(ws_auth, monkeypatch):
    async def lookup(session, digest):
        return key()

    monkeypatch.setattr(mcp_auth, "_lookup_by_digest", lookup)
    ws = FakeWebSocket()
    assert await auth_mod.authenticate_websocket(ws) == auth_mod._acw_principal(
        key(), scoped_service_tokens_enabled=False
    )
    assert ws.closed == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["unknown", "expired", "scope"])
async def test_invalid_key_closes(ws_auth, monkeypatch, failure):
    async def lookup(session, digest):
        if failure == "scope":
            return key(scopes=frozenset({"mcp:read"}))
        raise mcp_auth.MCPAuthError(
            "Invalid API key" if failure == "unknown" else "API key has expired"
        )

    monkeypatch.setattr(mcp_auth, "_lookup_by_digest", lookup)
    ws = FakeWebSocket()
    with pytest.raises(auth_mod.WebSocketAuthError):
        await auth_mod.authenticate_websocket(ws)
    assert ws.closed == [{"code": 4001, "reason": "Unauthorized"}]


@pytest.mark.asyncio
async def test_ownerless_key_closes(ws_auth, monkeypatch):
    async def lookup(session, digest):
        return key(user_id=None)

    monkeypatch.setattr(mcp_auth, "_lookup_by_digest", lookup)
    ws = FakeWebSocket()
    with pytest.raises(auth_mod.WebSocketAuthError):
        await auth_mod.authenticate_websocket(ws)
    assert ws.closed[0]["code"] == 4001


@pytest.mark.asyncio
async def test_validator_exception_closes(ws_auth, monkeypatch):
    async def broken(header):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(mcp_auth, "authenticate_rest", broken)
    ws = FakeWebSocket()
    with pytest.raises(auth_mod.WebSocketAuthError):
        await auth_mod.authenticate_websocket(ws)
    assert ws.closed[0]["code"] == 4001


@pytest.mark.asyncio
async def test_jwt_and_legacy_paths_still_work(ws_auth, monkeypatch):
    ws = FakeWebSocket()
    monkeypatch.setattr(
        auth_mod, "_try_decode_jwt", lambda token: {"sub": "jwt-user", "email": "e"}
    )
    assert await auth_mod.authenticate_websocket(ws) == {
        "id": "jwt-user",
        "email": "e",
        "role": "user",
    }
    monkeypatch.setattr(auth_mod, "_try_decode_jwt", lambda token: None)
    ws.query_params = {"token": "legacy"}
    assert await auth_mod.authenticate_websocket(ws) is None
    assert ws.closed == []


@pytest.mark.asyncio
@pytest.mark.parametrize("membership_error", [False, True])
async def test_event_endpoint_narrows_key_to_its_org(
    ws_auth, monkeypatch, membership_error
):
    membership_queried = False

    async def lookup(session, digest):
        return key()

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def execute(self, statement):
            nonlocal membership_queried
            membership_queried = True
            if membership_error:
                raise RuntimeError("membership lookup failed")
            return SimpleNamespace(all=lambda: [("A",), ("B",)])

    monkeypatch.setattr(mcp_auth, "_lookup_by_digest", lookup)
    monkeypatch.setattr(engine, "async_session_factory", Session)
    event_bus._clients.clear()
    event_bus.active_connections.clear()

    class EndpointWebSocket(FakeWebSocket):
        headers = {"authorization": "Bearer acw_test"}
        query_params = {}

        def __init__(self):
            super().__init__()
            self.sent = []
            self.disconnect = asyncio.Event()

        async def accept(self):
            pass

        async def receive_text(self):
            await self.disconnect.wait()
            raise WebSocketDisconnect()

        async def send_text(self, message):
            self.sent.append(json.loads(message))

    async def wait_for_registration():
        for _ in range(100):
            if event_bus._clients and membership_queried:
                return
            await asyncio.sleep(0.01)
        raise AssertionError("WebSocket client was not registered")

    ws = EndpointWebSocket()
    task = asyncio.create_task(events.events_websocket(ws))
    try:
        await wait_for_registration()
        await event_bus.deliver_local(OrgScope("A"), "from-a", {})
        await event_bus.deliver_local(OrgScope("B"), "from-b", {})
        assert ws.sent == (
            [] if membership_error else [{"type": "from-a", "payload": {}}]
        )
    finally:
        ws.disconnect.set()
        await task
