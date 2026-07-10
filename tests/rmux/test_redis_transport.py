"""RFC-0017 #681 — Redis-backed rmux transport tests.

Unit-level, no real Redis: a tiny in-memory fake stands in for the
``redis.asyncio`` client (hash ops + pub/sub). These assert the two properties
the RFC needs:

  * the panes index round-trips through Redis (register / get / list / unregister)
    so any replica can resolve a session it didn't create;
  * pane bytes published on a session's channel are received by a subscriber
    (the cross-replica fan-out), with raw bytes intact;

and the gating contract: with ``REDIS_URL`` unset every entry point is an inert
no-op (returns None / {} / does nothing), preserving pod-local behaviour.
"""

from __future__ import annotations

import asyncio

import pytest
from server.config import get_settings
from server.rmux import redis_transport as rt

pytestmark = pytest.mark.asyncio


class _FakePubSub:
    def __init__(self, hub: _FakeRedis) -> None:
        self._hub = hub
        self._queue: asyncio.Queue = asyncio.Queue()
        self._channel: str | None = None

    async def subscribe(self, channel: str) -> None:
        self._channel = channel
        self._hub.subscribers.setdefault(channel, []).append(self._queue)

    async def listen(self):
        # First a subscribe-confirmation frame (ignored by the consumer), then
        # message frames as they arrive.
        yield {"type": "subscribe", "data": 1}
        while True:
            data = await self._queue.get()
            yield {"type": "message", "data": data}

    async def unsubscribe(self, channel: str) -> None:
        subs = self._hub.subscribers.get(channel, [])
        if self._queue in subs:
            subs.remove(self._queue)

    async def aclose(self) -> None:
        return None


class _FakeRedis:
    """In-memory stand-in for redis.asyncio: hash ops + pub/sub."""

    def __init__(self) -> None:
        self.hashes: dict[str, dict[bytes, bytes]] = {}
        self.subscribers: dict[str, list[asyncio.Queue]] = {}

    async def hset(self, key: str, field: str, value: bytes) -> None:
        self.hashes.setdefault(key, {})[field.encode()] = value

    async def hdel(self, key: str, field: str) -> None:
        self.hashes.get(key, {}).pop(field.encode(), None)

    async def hget(self, key: str, field: str) -> bytes | None:
        return self.hashes.get(key, {}).get(field.encode())

    async def hgetall(self, key: str) -> dict[bytes, bytes]:
        return dict(self.hashes.get(key, {}))

    async def publish(self, channel: str, data: bytes) -> None:
        for q in self.subscribers.get(channel, []):
            q.put_nowait(data)

    def pubsub(self) -> _FakePubSub:
        return _FakePubSub(self)

    async def close(self) -> None:
        return None

    async def aclose(self) -> None:
        return None


@pytest.fixture
def fake_redis(monkeypatch):
    """Enable REDIS_URL + inject the in-memory fake as the transport client."""
    settings = get_settings()
    monkeypatch.setattr(settings, "REDIS_URL", "redis://fake:6379/0", raising=False)
    hub = _FakeRedis()
    monkeypatch.setattr(rt, "_redis_client", hub, raising=False)

    # subscribe_pane_bytes builds its own client via redis_asyncio.from_url —
    # route that to the same hub so publishes are visible to subscribers.
    import redis.asyncio as redis_asyncio

    monkeypatch.setattr(redis_asyncio, "from_url", lambda *_a, **_k: hub, raising=True)
    yield hub
    rt._redis_client = None


async def test_disabled_when_redis_url_unset(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "REDIS_URL", "", raising=False)
    monkeypatch.setattr(rt, "_redis_client", None, raising=False)

    assert rt.redis_enabled() is False
    # Every entry point is an inert no-op when disabled.
    await rt.register_pane("007", {"project_id": "p"})
    assert await rt.get_pane("007") is None
    assert await rt.list_panes() == {}
    await rt.publish_pane_bytes("007", b"data")  # no raise
    # subscribe yields nothing and returns immediately.
    got = [chunk async for chunk in rt.subscribe_pane_bytes("007")]
    assert got == []


async def test_panes_index_round_trip(fake_redis):
    assert rt.redis_enabled() is True
    await rt.register_pane(
        "007", {"spec_id": "007", "project_id": "proj-a", "passive": True}
    )
    entry = await rt.get_pane("007")
    assert entry == {"spec_id": "007", "project_id": "proj-a", "passive": True}

    await rt.register_pane("008", {"spec_id": "008", "project_id": "proj-b"})
    panes = await rt.list_panes()
    assert set(panes) == {"007", "008"}
    assert panes["008"]["project_id"] == "proj-b"

    await rt.unregister_pane("007")
    assert await rt.get_pane("007") is None
    assert set(await rt.list_panes()) == {"008"}


async def test_publish_reaches_subscriber(fake_redis):
    received: list[bytes] = []
    ready = asyncio.Event()

    async def _consume() -> None:
        async for chunk in rt.subscribe_pane_bytes("007"):
            received.append(chunk)
            if len(received) >= 2:
                return

    task = asyncio.create_task(_consume())
    # Give the subscriber a tick to register on the channel.
    for _ in range(50):
        await asyncio.sleep(0)
        if fake_redis.subscribers.get("aifactory:rmux:bytes:007"):
            ready.set()
            break
    assert ready.is_set(), "subscriber did not register on the channel"

    await rt.publish_pane_bytes("007", b"hello\r\n")
    await rt.publish_pane_bytes("007", b"\x1b[32mgreen\x1b[0m")
    await asyncio.wait_for(task, timeout=2.0)

    # Raw bytes (incl. ANSI escapes) survive the round-trip intact.
    assert received == [b"hello\r\n", b"\x1b[32mgreen\x1b[0m"]


async def test_publish_empty_is_noop(fake_redis):
    # Empty payloads never hit the bus (no subscriber needed).
    await rt.publish_pane_bytes("007", b"")  # no raise, no publish
    assert "aifactory:rmux:bytes:007" not in fake_redis.subscribers


async def test_session_lifecycle_mirrors_redis_index(fake_redis, tmp_path):
    """create → registers in the shared index; reap → removes it (#681)."""
    from server.rmux.session import SessionRegistry

    reg = SessionRegistry(panes_dir=tmp_path / "panes")
    await reg.create_passive_for_task("007", project_id="proj-a")

    entry = await rt.get_pane("007")
    assert entry is not None
    assert entry["project_id"] == "proj-a"
    assert entry["passive"] is True

    await reg.reap_for_task("007")
    assert await rt.get_pane("007") is None


async def test_feed_publishes_to_redis_channel(fake_redis, tmp_path):
    """A passive session's feed() mirrors bytes onto the shared bus (#681)."""
    from server.rmux.session import SessionRegistry

    reg = SessionRegistry(panes_dir=tmp_path / "panes")
    await reg.create_passive_for_task("007", project_id="proj-a")

    received: list[bytes] = []

    async def _consume() -> None:
        async for chunk in rt.subscribe_pane_bytes("007"):
            received.append(chunk)
            return

    task = asyncio.create_task(_consume())
    for _ in range(50):
        await asyncio.sleep(0)
        if fake_redis.subscribers.get("aifactory:rmux:bytes:007"):
            break

    # feed() schedules the Redis publish fire-and-forget on the running loop.
    reg.feed("007", b"line\r\n")
    await asyncio.wait_for(task, timeout=2.0)
    assert received == [b"line\r\n"]

    await reg.reap_for_task("007")
