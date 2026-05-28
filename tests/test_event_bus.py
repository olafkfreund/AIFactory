"""Tests for the cross-replica event bus (Epic #35 #40 PR-1).

Covers:
- ``publish_event`` delivers locally regardless of Redis state
- Echo suppression: subscriber skips envelopes published by self
- Scope filtering matches v1.0 behavior for broadcast/user/org
- Envelope ``v`` mismatch is logged and skipped
- Malformed envelopes don't crash the subscriber
- Three public shims in ``events.py`` still route through the bus
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

_WEB_SERVER = Path(__file__).parent.parent / "apps" / "web-server"
if str(_WEB_SERVER) not in sys.path:
    sys.path.insert(0, str(_WEB_SERVER))


from server.websockets import event_bus
from server.websockets.event_bus import (
    BroadcastScope,
    ConnectedClient,
    OrgScope,
    UserScope,
    _parse_envelope,
    _serialize_envelope,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fresh_registry():
    """Clear the registry between tests so each test starts clean."""
    event_bus._clients.clear()
    event_bus.active_connections.clear()


class _FakeWS:
    """Minimal WebSocket stand-in. Records sent messages, can simulate
    a send failure for the disconnected-client cleanup path."""

    def __init__(self, *, fail: bool = False):
        self.sent: list[str] = []
        self._fail = fail

    async def send_text(self, msg: str) -> None:
        if self._fail:
            raise RuntimeError("simulated disconnect")
        self.sent.append(msg)


def _register(ws, user_id: str | None = None, org_ids: set[str] | None = None):
    """Bypass the real WebSocket signature for unit tests."""
    client = ConnectedClient(
        websocket=ws,
        user_id=user_id,
        org_ids=org_ids or set(),
    )
    event_bus._clients[ws] = client
    event_bus.active_connections.add(ws)
    return client


# ---------------------------------------------------------------------------
# Envelope (de)serialization
# ---------------------------------------------------------------------------


def test_envelope_broadcast_round_trip():
    raw = _serialize_envelope(BroadcastScope(), "task:log", {"k": "v"})
    parsed = _parse_envelope(raw)
    assert parsed is not None
    source, scope, event_type, payload = parsed
    assert source == event_bus.self_replica_id
    assert isinstance(scope, BroadcastScope)
    assert event_type == "task:log"
    assert payload == {"k": "v"}


def test_envelope_user_round_trip():
    raw = _serialize_envelope(UserScope(user_id="u-42"), "notification", {})
    _, scope, _, _ = _parse_envelope(raw)
    assert isinstance(scope, UserScope) and scope.user_id == "u-42"


def test_envelope_org_round_trip():
    raw = _serialize_envelope(OrgScope(org_id="org-99"), "x", {})
    _, scope, _, _ = _parse_envelope(raw)
    assert isinstance(scope, OrgScope) and scope.org_id == "org-99"


def test_envelope_unknown_version_returns_none(caplog):
    """v mismatch is non-fatal: log + skip, don't raise. Ensures rolling
    upgrades survive when an older replica sees a future envelope."""
    raw = json.dumps({
        "v": 999, "source": "x",
        "scope": {"kind": "broadcast"},
        "type": "t", "payload": {},
    })
    import logging
    logging.getLogger("server.websockets.event_bus").propagate = True
    with caplog.at_level("WARNING"):
        assert _parse_envelope(raw) is None
    assert any("version" in r.message.lower() for r in caplog.records)


def test_envelope_malformed_json_returns_none():
    assert _parse_envelope(b"not-json") is None


def test_envelope_unknown_scope_kind_returns_none():
    raw = json.dumps({
        "v": 1, "source": "x",
        "scope": {"kind": "bogus"},
        "type": "t", "payload": {},
    })
    assert _parse_envelope(raw) is None


# ---------------------------------------------------------------------------
# Local delivery — scope filtering matches v1.0 behavior exactly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deliver_local_broadcast_hits_every_client():
    _fresh_registry()
    a, b, c = _FakeWS(), _FakeWS(), _FakeWS()
    _register(a, user_id="u1")
    _register(b, user_id=None)  # legacy
    _register(c, user_id="u2")

    await event_bus.deliver_local(BroadcastScope(), "t", {"x": 1})

    for ws in (a, b, c):
        assert len(ws.sent) == 1
        assert json.loads(ws.sent[0]) == {"type": "t", "payload": {"x": 1}}


@pytest.mark.asyncio
async def test_deliver_local_user_only_hits_matching_user():
    _fresh_registry()
    a, b = _FakeWS(), _FakeWS()
    _register(a, user_id="u-target")
    _register(b, user_id="u-other")

    await event_bus.deliver_local(UserScope(user_id="u-target"), "t", {})

    assert len(a.sent) == 1
    assert len(b.sent) == 0


@pytest.mark.asyncio
async def test_deliver_local_org_hits_members_plus_legacy():
    """Mirror the v1.0 OrgScope semantics: org members OR legacy
    (no-user_id) clients get the event."""
    _fresh_registry()
    member = _FakeWS()
    non_member = _FakeWS()
    legacy = _FakeWS()
    _register(member, user_id="u1", org_ids={"org-A"})
    _register(non_member, user_id="u2", org_ids={"org-B"})
    _register(legacy, user_id=None)

    await event_bus.deliver_local(OrgScope(org_id="org-A"), "t", {})

    assert len(member.sent) == 1
    assert len(non_member.sent) == 0
    assert len(legacy.sent) == 1  # back-compat


@pytest.mark.asyncio
async def test_deliver_local_cleans_up_disconnected():
    """Sends to a failed client get the client unregistered. Otherwise
    we'd keep iterating dead connections forever."""
    _fresh_registry()
    alive = _FakeWS()
    dead = _FakeWS(fail=True)
    _register(alive)
    _register(dead)

    await event_bus.deliver_local(BroadcastScope(), "t", {})

    assert alive in event_bus.active_connections
    assert dead not in event_bus.active_connections


# ---------------------------------------------------------------------------
# publish_event — local-first, Redis-optional
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_event_delivers_locally_when_redis_unset(monkeypatch):
    """REDIS_URL unset → publish_event = deliver_local. No Redis calls."""
    _fresh_registry()
    from server.config import get_settings
    monkeypatch.setattr(get_settings(), "REDIS_URL", "")
    monkeypatch.setattr(event_bus, "_redis_publisher", None)

    ws = _FakeWS()
    _register(ws)

    await event_bus.publish_event(BroadcastScope(), "t", {"v": 1})
    assert len(ws.sent) == 1


@pytest.mark.asyncio
async def test_publish_event_delivers_locally_AND_publishes_to_redis(monkeypatch):
    """REDIS_URL set → publish_event calls deliver_local first, then
    publishes the envelope to the configured channel."""
    _fresh_registry()
    from server.config import get_settings
    monkeypatch.setattr(get_settings(), "REDIS_URL", "redis://test/0")
    monkeypatch.setattr(get_settings(), "REDIS_CHANNEL", "test-channel")

    fake_pub = AsyncMock()
    fake_pub.publish = AsyncMock()
    monkeypatch.setattr(event_bus, "_get_redis_publisher", lambda: fake_pub)

    ws = _FakeWS()
    _register(ws, user_id="u-42")

    await event_bus.publish_event(UserScope(user_id="u-42"), "t", {})

    # Local delivery happened
    assert len(ws.sent) == 1
    # Redis publish happened with the right channel + envelope
    fake_pub.publish.assert_awaited_once()
    args, _ = fake_pub.publish.call_args
    assert args[0] == "test-channel"
    envelope = json.loads(args[1])
    assert envelope["scope"] == {"kind": "user", "user_id": "u-42"}
    assert envelope["source"] == event_bus.self_replica_id


@pytest.mark.asyncio
async def test_publish_event_swallows_redis_publish_failure(monkeypatch):
    """Redis hiccup must not raise into the caller — local delivery
    already succeeded and we don't want a transient Redis blip to
    break an HTTP request that fired an event."""
    _fresh_registry()
    from server.config import get_settings
    monkeypatch.setattr(get_settings(), "REDIS_URL", "redis://test/0")

    fake_pub = AsyncMock()
    fake_pub.publish = AsyncMock(side_effect=RuntimeError("redis down"))
    monkeypatch.setattr(event_bus, "_get_redis_publisher", lambda: fake_pub)

    ws = _FakeWS()
    _register(ws)

    # Should NOT raise.
    await event_bus.publish_event(BroadcastScope(), "t", {})
    assert len(ws.sent) == 1  # local delivery still happened


# ---------------------------------------------------------------------------
# Echo suppression — _dispatch_envelope skips self-published messages
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_envelope_skips_self_published():
    """Without echo-suppression, every published event would deliver
    twice on the publishing replica (once via deliver_local, once via
    the subscriber loop receiving its own publish)."""
    _fresh_registry()
    ws = _FakeWS()
    _register(ws)

    own_envelope = _serialize_envelope(BroadcastScope(), "t", {})
    await event_bus._dispatch_envelope(own_envelope)

    assert ws.sent == []  # not delivered — it's our own echo


@pytest.mark.asyncio
async def test_dispatch_envelope_delivers_when_source_differs():
    """Envelope from another replica → dispatch via deliver_local."""
    _fresh_registry()
    ws = _FakeWS()
    _register(ws, user_id="u-1")

    # Craft an envelope as if from a different replica.
    foreign = json.dumps({
        "v": 1,
        "source": "another-replica-uuid",
        "scope": {"kind": "user", "user_id": "u-1"},
        "type": "task:log",
        "payload": {"foo": "bar"},
    })
    await event_bus._dispatch_envelope(foreign)

    assert len(ws.sent) == 1
    msg = json.loads(ws.sent[0])
    assert msg == {"type": "task:log", "payload": {"foo": "bar"}}


# ---------------------------------------------------------------------------
# Public shims in events.py still route through the bus
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_events_shims_route_through_bus(monkeypatch):
    """The three public functions in events.py must call
    publish_event with the right scope. Captures the call to avoid
    touching Redis."""
    from server.websockets import events as events_mod

    captured: list[tuple] = []

    async def _fake_publish(scope, event_type, payload):
        captured.append((scope, event_type, payload))

    monkeypatch.setattr(event_bus, "publish_event", _fake_publish)

    await events_mod.broadcast_event("a", {"x": 1})
    await events_mod.send_to_user("u-1", "b", {"y": 2})
    await events_mod.send_to_org("org-9", "c", {"z": 3})

    assert len(captured) == 3
    assert isinstance(captured[0][0], BroadcastScope)
    assert captured[0][1:] == ("a", {"x": 1})
    assert captured[1][0] == UserScope(user_id="u-1")
    assert captured[1][1:] == ("b", {"y": 2})
    assert captured[2][0] == OrgScope(org_id="org-9")
    assert captured[2][1:] == ("c", {"z": 3})
