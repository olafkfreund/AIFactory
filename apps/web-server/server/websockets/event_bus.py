"""Cross-replica event bus for the WebSocket layer (Epic #35 #40 PR-1).

Wraps the in-process client registry + delivery loop with an optional
Redis pub/sub bridge so events fired on one replica reach WebSocket
clients connected to any other replica.

## Optional Redis

When ``settings.REDIS_URL`` is empty the bus is a thin in-process
shim: ``publish_event`` directly calls ``deliver_local``. This is the
laptop / dev / single-replica default and preserves v1.0 behavior
exactly.

When ``settings.REDIS_URL`` is set, ``publish_event`` ALSO publishes a
JSON envelope to ``settings.REDIS_CHANNEL``. A background subscriber
task on each replica receives those envelopes and dispatches them
into the local delivery loop — except for envelopes it published
itself (echo-suppressed by ``source_replica_id``).

Local delivery is **synchronous and independent of Redis**: own
replica's clients always receive own-replica events even during a
Redis outage. The cost is that other replicas' clients miss events
fired during the outage (at-most-once, no replay) — matches WS
semantics already.

## Module surface (used by ``events.py`` + ``main.py``)

- ``self_replica_id`` — UUID generated once at import, used for echo
  suppression and logged at startup so operators can correlate Redis
  traffic to specific pod instances
- ``Scope`` — tagged union of ``BroadcastScope`` / ``UserScope`` /
  ``OrgScope``; mirrors the three existing public functions in
  ``events.py``
- ``register_client`` / ``unregister_client`` / ``update_client_orgs``
  — connection registry, moved from ``events.py``
- ``ConnectedClient`` / ``_clients`` / ``active_connections`` —
  registry storage, also moved
- ``publish_event(scope, type, payload)`` — hot path
- ``deliver_local(scope, type, payload)`` — local-only delivery
- ``start_redis_subscriber()`` / ``stop_redis_subscriber()`` —
  lifecycle hooks called by ``main.py`` ``lifespan``
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field

from fastapi import WebSocket

from ..config import get_settings

logger = logging.getLogger(__name__)

# One UUID per app-instance. Stable for the process lifetime; new on
# every pod restart. Logged at startup by the lifespan hook.
self_replica_id: str = str(uuid.uuid4())

# Envelope wire-format version. Bump when adding fields that older
# readers shouldn't silently ignore. Older readers logged a warning
# and skip on mismatch.
ENVELOPE_VERSION = 1

# ---------------------------------------------------------------------------
# Client registry (moved from events.py — unchanged shape)
# ---------------------------------------------------------------------------


@dataclass
class ConnectedClient:
    """A connected WebSocket client with optional identity."""

    websocket: WebSocket
    user_id: str | None = None
    org_ids: set[str] = field(default_factory=set)


_clients: dict[WebSocket, ConnectedClient] = {}

# Legacy set kept for backward compat with any code that imported
# ``active_connections`` directly from events.py before this refactor.
active_connections: set[WebSocket] = set()


def register_client(ws: WebSocket, user_info: dict | None) -> ConnectedClient:
    """Register a new client connection.

    Called by the WS endpoint after authentication. ``user_info`` is
    the dict returned by ``authenticate_websocket`` (None for legacy
    bearer-token connections).
    """
    client = ConnectedClient(
        websocket=ws,
        user_id=user_info["id"] if user_info else None,
    )
    _clients[ws] = client
    active_connections.add(ws)
    return client


def unregister_client(ws: WebSocket) -> None:
    """Remove a client connection."""
    _clients.pop(ws, None)
    active_connections.discard(ws)


def update_client_orgs(user_id: str, org_ids: set[str]) -> None:
    """Update the org memberships for all connections of a given user.

    Call this after the user's org memberships change so routing
    reflects the new state.
    """
    for client in _clients.values():
        if client.user_id == user_id:
            client.org_ids = org_ids


# ---------------------------------------------------------------------------
# Scope tagged union (mirrors the three public functions in events.py)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BroadcastScope:
    """Send to every connected client. Mirrors ``broadcast_event``."""


@dataclass(frozen=True)
class UserScope:
    """Send only to clients matching ``user_id``. Mirrors
    ``send_to_user``."""

    user_id: str


@dataclass(frozen=True)
class OrgScope:
    """Send to clients whose ``org_ids`` includes ``org_id`` OR to
    legacy (no-user_id) clients. Mirrors ``send_to_org`` exactly."""

    org_id: str


Scope = BroadcastScope | UserScope | OrgScope


# ---------------------------------------------------------------------------
# Envelope serialization
# ---------------------------------------------------------------------------


def _serialize_envelope(
    scope: Scope, event_type: str, payload: dict
) -> str:
    """Build the JSON envelope published to Redis."""
    if isinstance(scope, BroadcastScope):
        scope_obj: dict = {"kind": "broadcast"}
    elif isinstance(scope, UserScope):
        scope_obj = {"kind": "user", "user_id": scope.user_id}
    elif isinstance(scope, OrgScope):
        scope_obj = {"kind": "org", "org_id": scope.org_id}
    else:
        raise TypeError(f"Unknown scope type: {type(scope)!r}")

    return json.dumps(
        {
            "v": ENVELOPE_VERSION,
            "source": self_replica_id,
            "scope": scope_obj,
            "type": event_type,
            "payload": payload,
        }
    )


def _parse_envelope(raw: bytes | str) -> tuple[str, Scope, str, dict] | None:
    """Parse a Redis envelope, returning ``(source, scope, type, payload)``.

    Returns ``None`` for envelopes that should be skipped (wrong version,
    malformed JSON, unknown scope kind). The caller has already decoded
    Redis' message-type framing.
    """
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.warning(
            "Dropped malformed event-bus envelope (first 200B): %r",
            (raw[:200] if isinstance(raw, (bytes, str)) else raw),
        )
        return None

    if not isinstance(obj, dict):
        logger.warning("Dropped non-object event-bus envelope: %r", obj)
        return None

    version = obj.get("v")
    if version != ENVELOPE_VERSION:
        logger.warning(
            "Dropped event-bus envelope with unknown version %r (expected %d)",
            version,
            ENVELOPE_VERSION,
        )
        return None

    source = obj.get("source")
    scope_raw = obj.get("scope")
    event_type = obj.get("type")
    payload = obj.get("payload", {})

    if not isinstance(source, str) or not isinstance(event_type, str):
        logger.warning("Dropped event-bus envelope missing source/type: %r", obj)
        return None

    if not isinstance(scope_raw, dict):
        logger.warning("Dropped event-bus envelope with non-object scope: %r", obj)
        return None

    kind = scope_raw.get("kind")
    if kind == "broadcast":
        scope: Scope = BroadcastScope()
    elif kind == "user" and isinstance(scope_raw.get("user_id"), str):
        scope = UserScope(user_id=scope_raw["user_id"])
    elif kind == "org" and isinstance(scope_raw.get("org_id"), str):
        scope = OrgScope(org_id=scope_raw["org_id"])
    else:
        logger.warning("Dropped event-bus envelope with unknown scope kind: %r", scope_raw)
        return None

    return source, scope, event_type, payload


# ---------------------------------------------------------------------------
# Local delivery (the in-process loop — runs in BOTH modes)
# ---------------------------------------------------------------------------


async def deliver_local(scope: Scope, event_type: str, payload: dict) -> None:
    """Deliver an event to local clients matching ``scope``.

    Mirrors the v1.0 routing rules exactly:
    - ``BroadcastScope``: every connected client
    - ``UserScope``: clients with matching ``user_id``
    - ``OrgScope``: clients whose ``org_ids`` includes the org OR
      legacy clients without a ``user_id`` (back-compat)
    """
    message = json.dumps({"type": event_type, "payload": payload})
    disconnected: list[WebSocket] = []

    if isinstance(scope, BroadcastScope):
        targets = list(active_connections)
        for ws in targets:
            try:
                await ws.send_text(message)
            except Exception:
                disconnected.append(ws)
    elif isinstance(scope, UserScope):
        for ws, client in list(_clients.items()):
            if client.user_id == scope.user_id:
                try:
                    await ws.send_text(message)
                except Exception:
                    disconnected.append(ws)
    elif isinstance(scope, OrgScope):
        for ws, client in list(_clients.items()):
            # Match v1.0: send to org members OR legacy (no user_id)
            # clients so bearer-token connections still receive events.
            if client.user_id is None or scope.org_id in client.org_ids:
                try:
                    await ws.send_text(message)
                except Exception:
                    disconnected.append(ws)

    for ws in disconnected:
        unregister_client(ws)


# ---------------------------------------------------------------------------
# Publish (the hot path called from events.py shims)
# ---------------------------------------------------------------------------


async def publish_event(
    scope: Scope, event_type: str, payload: dict
) -> None:
    """Deliver to local clients immediately, then publish to Redis if configured.

    Local delivery happens FIRST and synchronously. A Redis outage
    only affects cross-replica delivery, never own-replica delivery —
    that's the whole point of the layered model.

    Redis publishes are wrapped in try/except so a transient Redis
    hiccup never raises into the caller. Failures log at DEBUG to
    avoid flooding when Redis is sustainedly unreachable.
    """
    await deliver_local(scope, event_type, payload)

    redis_client = _get_redis_publisher()
    if redis_client is None:
        return  # in-process-only mode

    settings = get_settings()
    try:
        envelope = _serialize_envelope(scope, event_type, payload)
        await redis_client.publish(settings.REDIS_CHANNEL, envelope)
    except Exception:
        logger.debug(
            "Failed to publish event %r to Redis", event_type, exc_info=True
        )


# ---------------------------------------------------------------------------
# Redis lifecycle (subscriber + publisher)
# ---------------------------------------------------------------------------

_redis_publisher = None  # type: ignore[assignment]
_redis_subscriber_task: asyncio.Task | None = None
_RECONNECT_BACKOFF_MIN = 1.0
_RECONNECT_BACKOFF_MAX = 30.0


def _get_redis_publisher():
    """Return the cached publisher connection, or None when not configured.

    The publisher is created lazily on first publish so unit tests that
    don't exercise Redis don't need to mock anything.
    """
    global _redis_publisher
    settings = get_settings()
    if not settings.REDIS_URL:
        return None
    if _redis_publisher is None:
        import redis.asyncio as redis_asyncio
        _redis_publisher = redis_asyncio.from_url(
            settings.REDIS_URL, decode_responses=True
        )
    return _redis_publisher


async def start_redis_subscriber() -> None:
    """Start the background subscriber task.

    Called from ``main.py`` app lifespan when ``REDIS_URL`` is set.
    Safe to call when ``REDIS_URL`` is unset — becomes a no-op so
    callers don't have to gate on the setting themselves.
    """
    global _redis_subscriber_task
    settings = get_settings()
    if not settings.REDIS_URL:
        return
    if _redis_subscriber_task is not None and not _redis_subscriber_task.done():
        return  # already running
    _redis_subscriber_task = asyncio.create_task(
        _subscriber_loop(), name="event-bus-redis-subscriber"
    )
    logger.info(
        "Redis pub/sub subscriber started — replica %s on channel %r",
        self_replica_id,
        settings.REDIS_CHANNEL,
    )


async def stop_redis_subscriber() -> None:
    """Stop the subscriber task and close the publisher connection."""
    global _redis_subscriber_task, _redis_publisher
    if _redis_subscriber_task is not None:
        _redis_subscriber_task.cancel()
        try:
            await _redis_subscriber_task
        except (asyncio.CancelledError, Exception):
            pass
        _redis_subscriber_task = None
    if _redis_publisher is not None:
        try:
            await _redis_publisher.close()
        except Exception:
            pass
        _redis_publisher = None


async def _subscriber_loop() -> None:
    """Long-running subscriber task with reconnect-with-backoff.

    Same backoff policy on startup-unreachable and mid-session drops:
    1s → 30s, capped. Each successful connection resets the backoff.
    """
    import redis.asyncio as redis_asyncio

    settings = get_settings()
    backoff = _RECONNECT_BACKOFF_MIN

    while True:
        try:
            client = redis_asyncio.from_url(settings.REDIS_URL, decode_responses=True)
            pubsub = client.pubsub()
            await pubsub.subscribe(settings.REDIS_CHANNEL)
            backoff = _RECONNECT_BACKOFF_MIN  # reset after a healthy connect
            logger.info(
                "Subscribed to Redis channel %r", settings.REDIS_CHANNEL
            )

            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                await _dispatch_envelope(message.get("data"))

        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "Redis subscriber dropped — reconnecting in %.1fs", backoff,
                exc_info=True,
            )
            try:
                await asyncio.sleep(backoff)
            except asyncio.CancelledError:
                raise
            backoff = min(backoff * 2, _RECONNECT_BACKOFF_MAX)


async def _dispatch_envelope(raw: bytes | str | None) -> None:
    """Parse an envelope and dispatch to local delivery if it's not ours."""
    if raw is None:
        return
    parsed = _parse_envelope(raw)
    if parsed is None:
        return
    source, scope, event_type, payload = parsed
    if source == self_replica_id:
        return  # echo from our own publish; deliver_local already ran
    await deliver_local(scope, event_type, payload)
