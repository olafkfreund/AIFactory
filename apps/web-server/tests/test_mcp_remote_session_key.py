"""An SSE session only accepts messages from the key that opened it (#1910)."""

import json
import re
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock

import anyio
import pytest
from fastapi import FastAPI, HTTPException, Request
from httpx import ASGITransport, AsyncClient
from mcp.types import LATEST_PROTOCOL_VERSION
from server.mcp_remote import server as srv
from server.mcp_remote.auth import AuthenticatedKey

SID = "0123456789abcdef0123456789abcdef"


def _key(key_id: str) -> AuthenticatedKey:
    return AuthenticatedKey(
        key_id=key_id, scopes=frozenset({"mcp:write"}), user_id="u", org_id="o"
    )


async def test_post_from_another_key_is_refused(monkeypatch):
    keys = {"Bearer acw_a": _key("key-a"), "Bearer acw_b": _key("key-b")}

    async def authenticate(header):
        return keys[header]

    seen = {}

    @asynccontextmanager
    async def connect_sse(_scope, _receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        body = f"event: endpoint\r\ndata: /m/?session_id={SID}\r\n\r\n"
        await send({"type": "http.response.body", "body": body.encode()})
        seen["bound"] = srv._session_keys.get(SID)
        yield None, None

    async def run(*args):
        pass

    delivered = []

    async def handle_post_message(scope, _receive, _send):
        delivered.append(scope["query_string"])

    monkeypatch.setattr(srv, "authenticate", authenticate)
    monkeypatch.setattr(srv._sse_transport, "connect_sse", connect_sse)
    monkeypatch.setattr(srv._sse_transport, "handle_post_message", handle_post_message)
    monkeypatch.setattr(srv._mcp_server, "run", AsyncMock(side_effect=run))

    sent = []

    async def raw_send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/mcp-remote/sse",
        "headers": [(b"authorization", b"Bearer acw_a")],
        "query_string": b"",
    }
    await srv.sse_endpoint(Request(scope, AsyncMock(), raw_send))
    assert seen["bound"] == "key-a"
    assert len(sent) == 2  # the wrapper still forwards every message
    assert SID not in srv._session_keys  # dropped when the stream closes

    app = FastAPI()
    app.include_router(srv.router)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        srv._session_keys[SID] = "key-a"
        try:
            url = f"/api/mcp-remote/messages/?session_id={SID}"
            other = await client.post(url, headers={"Authorization": "Bearer acw_b"})
            own = await client.post(url, headers={"Authorization": "Bearer acw_a"})
            unknown = await client.post(
                "/api/mcp-remote/messages/?session_id=" + "f" * 32,
                headers={"Authorization": "Bearer acw_a"},
            )
        finally:
            srv._session_keys.pop(SID, None)

    assert other.status_code == 403
    assert own.status_code == 200
    assert unknown.status_code == 403
    assert len(delivered) == 1  # only the session's own key got through


async def _post(session_id: str, header: str, message: dict[str, Any]) -> int:
    body = json.dumps(message).encode()
    sent: list[dict[str, Any]] = []
    received = False

    async def receive_post():
        nonlocal received
        if not received:
            received = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send_post(message):
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/mcp-remote/messages/",
        "query_string": f"session_id={session_id}".encode(),
        "headers": [
            (b"authorization", header.encode()),
            (b"content-type", b"application/json"),
        ],
    }
    with anyio.fail_after(5):
        await srv.messages_endpoint(Request(scope, receive_post, send_post))
    return next(int(m["status"]) for m in sent if m["type"] == "http.response.start")


async def test_tool_call_over_the_real_transport_runs_as_the_session_key(monkeypatch):
    keys = {"Bearer acw_a": _key("key-a"), "Bearer acw_b": _key("key-b")}
    dispatched = []
    tool_name = "test_tool"

    async def authenticate(header):
        return keys[header]

    async def dispatch_tool_call(name, _arguments, key):
        dispatched.append((name, key.key_id))
        return {"content": [{"text": "ok"}]}

    monkeypatch.setattr(srv, "authenticate", authenticate)
    monkeypatch.setattr(srv, "dispatch_tool_call", dispatch_tool_call)

    disconnected = anyio.Event()
    endpoint_ready = anyio.Event()
    result_ready = anyio.Event()
    stream = bytearray()

    async def receive_sse():
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send_sse(message):
        if message["type"] == "http.response.body":
            stream.extend(message.get("body", b""))
            if re.search(rb"session_id=([0-9a-f]{32})", stream):
                endpoint_ready.set()
            if b'"id":2' in stream:
                result_ready.set()

    sse_scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/mcp-remote/sse",
        "headers": [(b"authorization", b"Bearer acw_a")],
        "query_string": b"",
    }

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(
            srv.sse_endpoint, Request(sse_scope, receive_sse, send_sse)
        )
        try:
            with anyio.fail_after(5):
                await endpoint_ready.wait()
            session_id = (
                re.search(rb"session_id=([0-9a-f]{32})", stream).group(1).decode()
            )

            assert (
                await _post(
                    session_id,
                    "Bearer acw_a",
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": LATEST_PROTOCOL_VERSION,
                            "capabilities": {},
                            "clientInfo": {"name": "test", "version": "1"},
                        },
                    },
                )
                == 202
            )
            assert (
                await _post(
                    session_id,
                    "Bearer acw_a",
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                )
                == 202
            )
            with pytest.raises(HTTPException) as exc:
                await _post(
                    session_id,
                    "Bearer acw_b",
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {"name": tool_name},
                    },
                )
            assert exc.value.status_code == 403
            assert (
                await _post(
                    session_id,
                    "Bearer acw_a",
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {"name": tool_name},
                    },
                )
                == 202
            )
            with anyio.fail_after(5):
                await result_ready.wait()
        finally:
            disconnected.set()

    assert dispatched == [(tool_name, "key-a")]
    assert b"missing auth context" not in stream
