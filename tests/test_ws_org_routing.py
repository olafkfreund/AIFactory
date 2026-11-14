"""Organization boundaries on the global event WebSocket."""

import asyncio
import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "apps" / "web-server"))
sys.path.insert(0, str(_ROOT / "apps" / "backend"))

from server import auth  # noqa: E402
from server.mcp_remote import auth as mcp_auth  # noqa: E402
from server.services import notification_service  # noqa: E402
from server.websockets import event_bus, events  # noqa: E402
from server.websockets.event_bus import BroadcastScope, OrgScope  # noqa: E402

_real_ws_acw_principal = auth._ws_acw_principal


def _engine():
    # Resolved at run time: other tests swap server.database.engine in sys.modules,
    # so an import-time handle goes stale (as in tests/test_ws_acw_auth.py).
    return importlib.import_module("server.database.engine")


@pytest.fixture
def sockets(monkeypatch):
    event_bus._clients.clear()
    event_bus.active_connections.clear()
    events._org_cache.clear()
    memberships = {"rows": [("A",)]}
    sent = {}

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def execute(self, _statement):
            return SimpleNamespace(all=lambda: memberships["rows"])

    monkeypatch.setattr(_engine(), "async_session_factory", Session)
    monkeypatch.setattr(
        auth,
        "get_settings",
        lambda: SimpleNamespace(DISABLE_AUTH=False, API_TOKEN="legacy"),
    )
    monkeypatch.setattr(
        auth,
        "_try_decode_jwt",
        lambda token: {"sub": token.removeprefix("jwt-")}
        if token.startswith("jwt-")
        else None,
    )
    monkeypatch.setattr(
        auth,
        "_ws_acw_principal",
        AsyncMock(return_value={"id": "owner", "org_id": "A"}),
    )
    monkeypatch.setattr(events, "_ws_acw_principal", auth._ws_acw_principal)
    monkeypatch.setattr(event_bus, "_get_redis_publisher", lambda: None)
    monkeypatch.setattr(
        events,
        "load_projects",
        lambda: {"p-a": {"org_id": "A"}, "p-b": {"org_id": "B"}},
    )

    original_send = WebSocket.send_text

    async def record_send(ws, message):
        token = (
            dict(ws.scope["headers"])
            .get(b"authorization", b"")
            .decode()
            .removeprefix("Bearer ")
        )
        sent.setdefault(token, []).append(json.loads(message))
        await original_send(ws, message)

    monkeypatch.setattr(WebSocket, "send_text", record_send)
    app = FastAPI()
    app.include_router(events.router)
    with TestClient(app) as client:
        yield client, memberships, sent
    event_bus._clients.clear()
    event_bus.active_connections.clear()


def connect(client, token):
    return client.websocket_connect(
        "/ws/events", headers={"authorization": f"Bearer {token}"}
    )


def emit(client, coro, *args):
    client.portal.call(coro, *args)


def test_project_events_confined_to_members_and_service(sockets):
    client, memberships, sent = sockets
    with connect(client, "jwt-a"), connect(client, "legacy"):
        memberships["rows"] = [("B",)]
        with connect(client, "jwt-b"):
            emit(client, events.broadcast_event, "event", {"projectId": "p-b"})
            assert sent.get("jwt-a", []) == []
            assert sent["jwt-b"][-1]["payload"] == {"projectId": "p-b"}
            assert sent["legacy"][-1]["payload"] == {"projectId": "p-b"}


def test_key_bound_to_org_even_with_two_memberships_and_update(sockets):
    client, memberships, sent = sockets
    memberships["rows"] = [("A",), ("B",)]
    with connect(client, "acw_key"):
        emit(client, events.broadcast_event, "event", {"projectId": "p-a"})
        emit(client, events.broadcast_event, "event", {"projectId": "p-b"})
        event_bus.update_client_orgs("owner", {"A", "B"})
        emit(client, events.broadcast_event, "event", {"projectId": "p-b"})
        assert [m["payload"]["projectId"] for m in sent["acw_key"]] == ["p-a"]
        assert next(iter(event_bus._clients.values())).key_org_id == "A"


@pytest.mark.parametrize(
    "payload", [{}, {"taskId": "bare-spec"}, {"projectId": "missing"}]
)
def test_unresolved_events_only_reach_service(sockets, payload):
    client, _memberships, sent = sockets
    with (
        connect(client, "jwt-a"),
        connect(client, "acw_key"),
        connect(client, "legacy"),
    ):
        emit(client, events.broadcast_event, "event", payload)
        assert sent.get("jwt-a", []) == sent.get("acw_key", []) == []
        assert sent["legacy"][-1]["payload"] == payload


def test_registry_exception_fails_closed(sockets, monkeypatch):
    client, _memberships, sent = sockets

    def broken():
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(events, "load_projects", broken)
    with connect(client, "jwt-a"), connect(client, "legacy"):
        emit(client, events.broadcast_event, "event", {"projectId": "p-a"})
        assert sent.get("jwt-a", []) == []
        assert len(sent["legacy"]) == 1


def test_user_event_requires_matching_org_for_key(sockets):
    client, _memberships, sent = sockets
    with connect(client, "acw_key"), connect(client, "jwt-owner"):
        emit(client, events.send_to_user, "owner", "cli-account-auth", {})
        emit(
            client,
            events.send_to_user,
            "owner",
            "notification",
            {"data": {"org_id": "B"}},
        )
        emit(client, events.send_to_user, "owner", "notification", {"org_id": "A"})
        # Membership gone (failed lookup or removal): the key socket gets nothing.
        event_bus.update_client_orgs("owner", set())
        emit(client, events.send_to_user, "owner", "notification", {"org_id": "A"})
        assert [m["type"] for m in sent["acw_key"]] == ["notification"]
        assert len(sent["jwt-owner"]) == 4


def test_legacy_envelopes_and_empty_org_round_trip(sockets):
    client, _memberships, sent = sockets
    with (
        connect(client, "jwt-a"),
        connect(client, "acw_key"),
        connect(client, "legacy"),
    ):
        for scope in (BroadcastScope(), OrgScope("")):
            parsed = event_bus._parse_envelope(
                event_bus._serialize_envelope(scope, "old", {})
            )
            assert parsed is not None
            emit(client, event_bus.deliver_local, parsed[1], parsed[2], parsed[3])
        assert sent.get("jwt-a", []) == sent.get("acw_key", []) == []
        assert len(sent["legacy"]) == 2


def test_real_task_status_routes_by_task_project(sockets):
    client, memberships, sent = sockets
    with connect(client, "jwt-a"):
        memberships["rows"] = [("B",)]
        with connect(client, "jwt-b"):
            emit(client, events.emit_task_status, "p-a:001-x", "running")
            assert sent["jwt-a"][-1] == {
                "type": "task:status",
                "payload": {"taskId": "p-a:001-x", "status": "running"},
            }
            assert sent.get("jwt-b", []) == []


def test_event_org_caches_none_and_reassignment(monkeypatch):
    events._org_cache.clear()
    now = [10.0]
    projects = {"p": {}}
    reads = []
    monkeypatch.setattr(events.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(events, "load_projects", lambda: reads.append(1) or projects)
    assert events._event_org({"projectId": "p"}) is None
    projects["p"]["org_id"] = "A"
    assert events._event_org({"projectId": "p"}) is None
    assert len(reads) == 1
    now[0] += 31
    assert events._event_org({"projectId": "p"}) == "A"
    projects["p"]["org_id"] = "B"
    now[0] += 31
    assert events._event_org({"projectId": "p"}) == "B"


@pytest.mark.parametrize("validator", [None, RuntimeError("validator failed")])
def test_key_revocation_closes_socket(sockets, monkeypatch, validator):
    client, _memberships, _sent = sockets
    key = mcp_auth.AuthenticatedKey("key-id", frozenset({"api"}), "owner", "A")
    monkeypatch.setattr(
        mcp_auth, "authenticate_rest", AsyncMock(side_effect=[key, validator])
    )
    monkeypatch.setattr(auth, "_ws_acw_principal", _real_ws_acw_principal)
    monkeypatch.setattr(events, "_ws_acw_principal", _real_ws_acw_principal)
    monkeypatch.setattr(events, "_KEY_RECHECK_SECONDS", 0)
    with connect(client, "acw_key") as ws:
        ws.send_text("ping")
        assert ws.receive()["code"] == 4001
    assert not event_bus._clients


def test_notify_org_overrides_data_org(sockets, monkeypatch):
    client, memberships, sent = sockets
    memberships["rows"] = [("A",)]
    service = notification_service.NotificationService()
    with connect(client, "acw_key"):
        with connect(client, "jwt-owner"):
            memberships["rows"] = [("owner",)]
            emit(
                client,
                service.notify_org,
                "A",
                "notice",
                "title",
                "message",
                {"org_id": "B"},
            )
            assert sent["acw_key"][-1]["payload"]["data"]["org_id"] == "A"
            assert sent["jwt-owner"][-1]["payload"]["data"]["org_id"] == "A"


def test_task_log_writer_stream_carries_project_id(sockets, tmp_path):
    from server.services.task_log_writer import TaskLogWriter
    from server.services.task_phase import TaskPhase

    client, memberships, sent = sockets
    writer = TaskLogWriter(tmp_path, project_id="p-a")

    async def add_and_flush():
        writer.add_entry("001-x", TaskPhase.CODING, "text", "hi")
        await asyncio.sleep(0.2)

    with connect(client, "jwt-a"):
        memberships["rows"] = [("B",)]
        with connect(client, "jwt-b"):
            client.portal.call(add_and_flush)
            assert sent["jwt-a"][-1]["type"] == "task-logs:stream"
            assert sent["jwt-a"][-1]["payload"]["projectId"] == "p-a"
            assert sent.get("jwt-b", []) == []


@pytest.mark.parametrize("event", ["cli-account-auth", "github:auth-complete"])
def test_org_less_login_events_follow_the_starting_principal(sockets, event):
    client, _memberships, sent = sockets
    with (
        connect(client, "jwt-owner"),
        connect(client, "jwt-other"),
        connect(client, "acw_key"),
        connect(client, "legacy"),
    ):
        emit(client, events.emit_org_less, {"id": "owner"}, event, {"ok": True})
        assert [m["type"] for m in sent["jwt-owner"]] == [event]
        assert sent.get("jwt-other", []) == sent.get("acw_key", []) == []
        assert sent.get("legacy", []) == []
        emit(
            client,
            events.emit_org_less,
            {"id": "default", "is_service": True},
            event,
            {},
        )
        emit(client, events.emit_org_less, None, event, {})
        assert len(sent["legacy"]) == 2
        assert len(sent["jwt-owner"]) == 1
        assert sent.get("jwt-other", []) == sent.get("acw_key", []) == []


def _no_database():
    raise RuntimeError("membership lookup unavailable")


def _wait_for_no_orgs(client, socket):
    for _ in range(200):
        if not socket.org_ids:
            return
        client.portal.call(asyncio.sleep, 0.01)
    raise AssertionError("memberships were not reloaded")


def test_key_socket_follows_membership_on_recheck(sockets, monkeypatch):
    client, _memberships, sent = sockets
    monkeypatch.setattr(events, "_KEY_RECHECK_SECONDS", 0)
    with connect(client, "acw_key") as ws:
        emit(client, events.broadcast_event, "event", {"projectId": "p-a"})
        assert len(sent["acw_key"]) == 1
        monkeypatch.setattr(_engine(), "async_session_factory", _no_database)
        ws.send_text("wake the receive loop")
        _wait_for_no_orgs(client, next(iter(event_bus._clients.values())))
        emit(client, events.broadcast_event, "event", {"projectId": "p-a"})
        assert len(sent["acw_key"]) == 1
