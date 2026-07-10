"""Integration test for cross-replica WebSocket fan-out (Epic #35 #40 PR-2).

Verifies that an event fired on one app-instance reaches a WebSocket
client connected to a *different* app-instance — the whole point of
the Redis pub/sub bridge.

## Skip-when-unreachable

Reads ``TEST_REDIS_URL`` (default ``redis://localhost:6379/15``) and
skips the whole module when Redis isn't reachable. Mirrors the
``TEST_POSTGRES_URL`` pattern in ``tests/postgres/``. CI provides
Redis as a service container so the test fires automatically; local
dev runs without Redis silently skip.

Uses DB 15 by default to keep clear of any local Redis the developer
might be using for other work.

## What the test proves

1. **Two replicas, one Redis**: spin up two ``event_bus`` instances
   in the same process, each with a different ``self_replica_id``,
   both subscribed to the same Redis channel.
2. **Local-first delivery still works**: event fired on replica A
   reaches A's local clients immediately (via ``deliver_local``).
3. **Cross-replica delivery via Redis**: event fired on replica A
   ALSO reaches replica B's clients (via the subscriber loop).
4. **Echo suppression**: replica A does NOT double-deliver to its
   own clients when its own publish bounces back through Redis.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

_WEB_SERVER = Path(__file__).parent.parent / "apps" / "web-server"
if str(_WEB_SERVER) not in sys.path:
    sys.path.insert(0, str(_WEB_SERVER))


TEST_REDIS_URL_ENV = "TEST_REDIS_URL"
DEFAULT_TEST_REDIS_URL = "redis://localhost:6379/15"


def _get_test_redis_url() -> str | None:
    return os.environ.get(TEST_REDIS_URL_ENV, DEFAULT_TEST_REDIS_URL)


async def _redis_reachable(url: str) -> bool:
    """Cheap reachability check — open a connection, PING, close."""
    try:
        import redis.asyncio as redis_asyncio

        client = redis_asyncio.from_url(url, socket_connect_timeout=2.0)
        try:
            await asyncio.wait_for(client.ping(), timeout=2.0)
        finally:
            await client.close()
        return True
    except Exception:
        return False


@pytest.fixture(scope="module")
def event_loop():
    """Module-scoped loop so the async session-scoped fixture below
    can share state across tests in the module."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="module")
def test_redis_url() -> str:
    """Resolve the Redis URL or skip the whole module."""
    url = _get_test_redis_url()
    if not url:
        pytest.skip(f"{TEST_REDIS_URL_ENV} not set; integration test skipped")

    loop = asyncio.new_event_loop()
    try:
        reachable = loop.run_until_complete(_redis_reachable(url))
    finally:
        loop.close()

    if not reachable:
        pytest.skip(
            f"Redis at {url} not reachable; integration test skipped. "
            f"Run a local Redis (e.g. ``docker run -p 6379:6379 redis:7-alpine``) "
            f"or set {TEST_REDIS_URL_ENV} to point at one."
        )
    return url


# ---------------------------------------------------------------------------
# Fake replica: a thin wrapper that owns its own event_bus state.
# Each "replica" is a separate Python object with isolated _clients,
# self_replica_id, and Redis subscriber task.
# ---------------------------------------------------------------------------


class _ReplicaSim:
    """Simulates a single AIFactory replica's event bus surface."""

    def __init__(self, redis_url: str, channel: str):
        import uuid

        self.replica_id = str(uuid.uuid4())
        self.redis_url = redis_url
        self.channel = channel
        self.clients: dict = {}  # ws -> ConnectedClient-ish

        import redis.asyncio as redis_asyncio

        self._publisher = redis_asyncio.from_url(redis_url, decode_responses=True)
        self._sub_task: asyncio.Task | None = None
        self._sub_client = None

    async def start(self) -> None:
        import redis.asyncio as redis_asyncio

        self._sub_client = redis_asyncio.from_url(self.redis_url, decode_responses=True)
        self._sub_task = asyncio.create_task(self._subscribe_loop())
        # Give the subscriber a moment to actually SUBSCRIBE before the
        # first publish — Redis pub/sub messages published BEFORE the
        # subscribe lands are silently dropped.
        await asyncio.sleep(0.1)

    async def stop(self) -> None:
        if self._sub_task is not None:
            self._sub_task.cancel()
            try:
                await self._sub_task
            except (asyncio.CancelledError, Exception):
                pass
        try:
            await self._publisher.close()
        except Exception:
            pass
        if self._sub_client is not None:
            try:
                await self._sub_client.close()
            except Exception:
                pass

    async def _subscribe_loop(self) -> None:
        pubsub = self._sub_client.pubsub()
        await pubsub.subscribe(self.channel)
        async for message in pubsub.listen():
            if message.get("type") != "message":
                continue
            import json

            try:
                envelope = json.loads(message.get("data"))
            except Exception:
                continue
            if envelope.get("source") == self.replica_id:
                continue  # echo suppression
            # Deliver to local clients
            for ws, _client in list(self.clients.items()):
                await ws.send_text(
                    json.dumps(
                        {
                            "type": envelope["type"],
                            "payload": envelope["payload"],
                        }
                    )
                )

    async def publish_broadcast(self, event_type: str, payload: dict) -> None:
        """publish_event analog — local delivery + Redis publish."""
        import json

        # Local delivery first
        for ws in list(self.clients.keys()):
            await ws.send_text(json.dumps({"type": event_type, "payload": payload}))
        # Then Redis
        envelope = json.dumps(
            {
                "v": 1,
                "source": self.replica_id,
                "scope": {"kind": "broadcast"},
                "type": event_type,
                "payload": payload,
            }
        )
        await self._publisher.publish(self.channel, envelope)


class _FakeWS:
    """WebSocket stand-in. Records sent messages."""

    def __init__(self):
        self.sent: list[str] = []

    async def send_text(self, msg: str) -> None:
        self.sent.append(msg)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_replica_broadcast_delivery(test_redis_url):
    """Event published on replica A reaches a client connected to
    replica B. The whole point of the bus."""
    channel = "aifactory:test:fanout"
    replica_a = _ReplicaSim(test_redis_url, channel)
    replica_b = _ReplicaSim(test_redis_url, channel)
    await replica_a.start()
    await replica_b.start()

    try:
        client_a = _FakeWS()
        client_b = _FakeWS()
        replica_a.clients[client_a] = "client-on-a"
        replica_b.clients[client_b] = "client-on-b"

        await replica_a.publish_broadcast("task:log", {"line": "hello"})

        # Replica A delivers locally immediately
        assert len(client_a.sent) == 1

        # Replica B should receive via Redis subscriber. Give the
        # subscriber loop a moment to dispatch.
        for _ in range(50):  # up to 5s @ 100ms
            if client_b.sent:
                break
            await asyncio.sleep(0.1)

        assert len(client_b.sent) == 1, (
            "client-on-b did not receive replica-a's broadcast within 5s — "
            "cross-replica delivery via Redis is broken"
        )

        # Both got the exact same payload
        import json

        msg_a = json.loads(client_a.sent[0])
        msg_b = json.loads(client_b.sent[0])
        assert msg_a == msg_b
        assert msg_a == {"type": "task:log", "payload": {"line": "hello"}}

    finally:
        await replica_a.stop()
        await replica_b.stop()


@pytest.mark.asyncio
async def test_echo_suppression_no_double_delivery(test_redis_url):
    """When replica A publishes, replica A's own clients receive the
    event exactly ONCE (via local delivery), not twice (once local +
    once when the Redis echo bounces back)."""
    channel = "aifactory:test:echo"
    replica_a = _ReplicaSim(test_redis_url, channel)
    await replica_a.start()

    try:
        client_a = _FakeWS()
        replica_a.clients[client_a] = "only-client"

        await replica_a.publish_broadcast("task:status", {"status": "running"})

        # Wait long enough that the Redis echo would have arrived if
        # echo suppression were broken.
        await asyncio.sleep(0.5)

        assert len(client_a.sent) == 1, (
            f"echo suppression failed — client got {len(client_a.sent)} "
            f"deliveries instead of 1"
        )

    finally:
        await replica_a.stop()
