"""RFC-0017 #681 — Redis-backed transport for the rmux Live Agent Console.

AIFactory is pinned to ``replicas: 1`` because the rmux console keeps two pieces
of state **pod-local**:

  * the **pane byte-stream** — agent output written into a Unix FIFO under
    ``AIFACTORY_RMUX_PANES_DIR``; the WebSocket bridge reads that FIFO. A FIFO
    only exists on the pod that created it, so a second replica can't stream a
    session it didn't start.
  * the **panes index** — the in-memory ``SessionRegistry._states`` dict, so
    only the pod that created a session knows it exists.

This module adds a **Redis-backed transport alongside** the pod-local one so any
replica can serve any console session:

  * pane bytes are published to ``aifactory:rmux:bytes:<spec_id>`` (Redis
    pub/sub) and any replica's WS bridge can subscribe to that channel;
  * the panes index lives in the ``aifactory:rmux:panes`` Redis hash keyed by
    ``spec_id``, so any replica can resolve a session it didn't create.

Gating + fallback (the whole point):

* Behind ``REDIS_URL`` (the SAME setting that already backs the cross-replica
  WebSocket event bus, ``websockets/event_bus.py``). When it is **unset** every
  function here is an inert no-op / returns ``None`` and the registry + bridge
  keep their exact pod-local behaviour — zero change to today's single-replica
  deployment.
* Best-effort: Redis hiccups are swallowed (logged at DEBUG); the local FIFO
  path is always present as the primary on the originating pod, so a Redis
  outage degrades cross-replica streaming, never local streaming.

Mirrors the ``event_bus`` Redis conventions: lazy ``redis.asyncio.from_url``
clients, reconnect-tolerant subscribe loop, ``decode_responses=False`` so raw
pane bytes survive the round-trip intact (xterm needs the bytes verbatim).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from factory_common.logsafe import sanitize_log

from ..config import get_settings

_log = logging.getLogger(__name__)

# Key layout — distinct ``aifactory:rmux:*`` prefix so console traffic never
# collides with the event bus's ``aifactory:events`` channel on a shared Redis.
_PANES_INDEX_KEY = "aifactory:rmux:panes"
_BYTES_CHANNEL_PREFIX = "aifactory:rmux:bytes:"


def redis_enabled() -> bool:
    """True when a shared Redis bus is configured (``REDIS_URL`` set).

    The single switch for the whole module: unset → every entry point is a
    no-op and the registry/bridge stay pod-local (today's behaviour).
    """
    try:
        return bool(get_settings().REDIS_URL)
    except Exception:  # noqa: BLE001 - settings unavailable → treat as disabled
        return False


def _channel(spec_id: str) -> str:
    return f"{_BYTES_CHANNEL_PREFIX}{spec_id}"


# ---------------------------------------------------------------------------
# Lazy clients (mirror event_bus: cached publisher, fresh subscriber per loop)
# ---------------------------------------------------------------------------

_redis_client: Any = None


def _get_client() -> Any:
    """Return the cached raw-bytes Redis client, or None when not configured.

    ``decode_responses=False`` so published pane bytes (ANSI escapes, UTF-8
    fragments) round-trip verbatim. Lazily created so tests that never touch
    Redis don't need a server.
    """
    global _redis_client
    settings = get_settings()
    if not settings.REDIS_URL:
        return None
    if _redis_client is None:
        import redis.asyncio as redis_asyncio

        _redis_client = redis_asyncio.from_url(
            settings.REDIS_URL, decode_responses=False
        )
    return _redis_client


async def close() -> None:
    """Close the cached client (lifecycle hook for app shutdown)."""
    global _redis_client
    if _redis_client is not None:
        try:
            await _redis_client.close()
        except Exception:  # noqa: BLE001 - best-effort shutdown
            _log.debug("[rmux-redis] client close failed", exc_info=True)
        _redis_client = None


# ---------------------------------------------------------------------------
# Panes index (Redis hash) — so any replica can resolve a session
# ---------------------------------------------------------------------------


async def register_pane(spec_id: str, meta: dict[str, Any]) -> None:
    """Record a session in the shared panes index. No-op when Redis is off.

    ``meta`` is a small JSON-able dict (project_id, session_name, the
    originating replica, passive flag). Never raises.
    """
    client = _get_client()
    if client is None:
        return
    try:
        await client.hset(_PANES_INDEX_KEY, spec_id, json.dumps(meta).encode())
    except Exception:  # noqa: BLE001 - index mirror is best-effort
        _log.debug("[rmux-redis] register_pane failed for %s", spec_id, exc_info=True)


async def unregister_pane(spec_id: str) -> None:
    """Remove a session from the shared panes index. No-op when Redis is off."""
    client = _get_client()
    if client is None:
        return
    try:
        await client.hdel(_PANES_INDEX_KEY, spec_id)
    except Exception:  # noqa: BLE001 - index mirror is best-effort
        _log.debug(
            "[rmux-redis] unregister_pane failed for %s",
            sanitize_log(spec_id),
            exc_info=True,
        )


async def get_pane(spec_id: str) -> dict[str, Any] | None:
    """Look up a session in the shared index, or None. No-op → None when off."""
    client = _get_client()
    if client is None:
        return None
    try:
        raw = await client.hget(_PANES_INDEX_KEY, spec_id)
    except Exception:  # noqa: BLE001 - index read is best-effort
        _log.debug(
            "[rmux-redis] get_pane failed for %s", sanitize_log(spec_id), exc_info=True
        )
        return None
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


async def list_panes() -> dict[str, dict[str, Any]]:
    """Return the whole shared panes index. Empty dict when Redis is off."""
    client = _get_client()
    if client is None:
        return {}
    try:
        raw = await client.hgetall(_PANES_INDEX_KEY)
    except Exception:  # noqa: BLE001 - index read is best-effort
        _log.debug("[rmux-redis] list_panes failed", exc_info=True)
        return {}
    out: dict[str, dict[str, Any]] = {}
    for key, val in (raw or {}).items():
        spec_id = key.decode() if isinstance(key, bytes) else str(key)
        try:
            parsed = json.loads(val)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(parsed, dict):
            out[spec_id] = parsed
    return out


# ---------------------------------------------------------------------------
# Pane byte fan-out (Redis pub/sub) — so any replica can stream any session
# ---------------------------------------------------------------------------


async def publish_pane_bytes(spec_id: str, data: bytes) -> None:
    """Publish pane bytes onto the session's Redis channel. No-op when off.

    Called alongside the local FIFO write so a replica that does NOT host the
    FIFO can still stream the session via ``subscribe_pane_bytes``. Best-effort:
    a Redis hiccup never affects the local FIFO path. Never raises.
    """
    if not data:
        return
    client = _get_client()
    if client is None:
        return
    try:
        await client.publish(_channel(spec_id), data)
    except Exception:  # noqa: BLE001 - fan-out is best-effort
        _log.debug(
            "[rmux-redis] publish_pane_bytes failed for %s",
            sanitize_log(spec_id),
            exc_info=True,
        )


async def subscribe_pane_bytes(spec_id: str) -> AsyncIterator[bytes]:
    """Yield pane bytes published to ``spec_id``'s channel until cancelled.

    The cross-replica counterpart of the local FIFO reader: a WS bridge on any
    replica can stream a session by subscribing here. Yields nothing (returns
    immediately) when Redis is off. Reconnect-tolerant — a dropped connection
    is retried with backoff, matching the event-bus subscriber, so a transient
    Redis blip doesn't end the console stream.
    """
    if not redis_enabled():
        return
    import redis.asyncio as redis_asyncio

    settings = get_settings()
    channel = _channel(spec_id)
    backoff = 1.0
    while True:
        client = None
        pubsub = None
        try:
            client = redis_asyncio.from_url(settings.REDIS_URL, decode_responses=False)
            pubsub = client.pubsub()
            await pubsub.subscribe(channel)
            backoff = 1.0
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                data = message.get("data")
                if isinstance(data, bytes) and data:
                    yield data
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - reconnect on any transport error
            _log.debug(
                "[rmux-redis] pane subscribe for %s dropped — retry in %ss",
                sanitize_log(spec_id),
                sanitize_log(backoff),
                exc_info=True,
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
        finally:
            if pubsub is not None:
                try:
                    await pubsub.unsubscribe(channel)
                    await pubsub.aclose()
                except Exception:  # noqa: BLE001 - best-effort cleanup
                    _log.debug("[rmux-redis] pubsub cleanup failed", exc_info=True)
            if client is not None:
                try:
                    await client.aclose()
                except Exception:  # noqa: BLE001 - best-effort cleanup
                    _log.debug("[rmux-redis] client cleanup failed", exc_info=True)
