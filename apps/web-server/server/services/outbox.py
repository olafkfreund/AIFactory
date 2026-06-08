"""Transactional outbox + retrying relay for completion-event delivery (#465).

**At-least-once delivery.** Today AIFactory emits the RFC-0001 completion event
(``completion.py``) fire-and-forget: if the process dies between the terminal
state change and a successful webhook POST — or the POST times out — the event is
lost and CFactory's WorkItem is silently wrong with no replay.

This module fixes that. The event is persisted to a durable SQLite outbox the
instant a task reaches a terminal state — *before* any network call — so a crash
can never lose it. A relay drains undelivered rows with exponential backoff and
marks each delivered on a 2xx. Idempotency is the consumer's job, keyed on the
envelope ``id`` (#466): a re-delivered row dedups cleanly on the other side, and
re-enqueueing the same event here is a no-op (the ``id`` is the primary key).

**Why SQLite, not the main DB.** AIFactory's terminal state change is a
*file-based* control-plane write (``task_control.write_control``), not a row in
the web-server's SQLAlchemy DB — so there is no SQL transaction to enlist in. A
dedicated stdlib ``sqlite3`` outbox gives us the property that actually matters
(durable, atomic, survives restart) with zero new dependencies and no migration
framework, matching the emitter's "stdlib-only, best-effort, never break the
pipeline" ethos.

Both halves are flag-gated by ``AIFACTORY_COMPLETION_OUTBOX``; until it is turned
on the existing direct-webhook path in ``completion.py`` is untouched, so this
ships without changing any current behaviour (epic #468: additive, behind a flag,
cut over once verified).
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = [
    "outbox_enabled",
    "db_path",
    "enqueue",
    "deliver_due_once",
    "pending_count",
    "relay_loop",
    "DeliveryError",
]

# Exponential backoff: 5s, 10s, 20s, … capped at 1h. Deterministic (no jitter)
# so the relay's behaviour is reproducible in tests; a single relay per instance
# means we don't need jitter to avoid a thundering herd.
_BACKOFF_BASE_S = 5.0
_BACKOFF_CAP_S = 3600.0

# A row is abandoned (left for human inspection) after this many failed attempts
# so a permanently-bad target can't be retried forever. ~ reaches the 1h cap.
_MAX_ATTEMPTS = 25

_SCHEMA = """
CREATE TABLE IF NOT EXISTS completion_outbox (
    id              TEXT PRIMARY KEY,
    url             TEXT NOT NULL,
    payload         TEXT NOT NULL,
    correlation_key TEXT,
    created_at      TEXT NOT NULL,
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL    NOT NULL DEFAULT 0,
    delivered_at    TEXT,
    last_error      TEXT
);
CREATE INDEX IF NOT EXISTS ix_outbox_pending
    ON completion_outbox (delivered_at, next_attempt_at);
"""


class DeliveryError(Exception):
    """A delivery attempt failed (transport error or non-2xx response)."""


def outbox_enabled() -> bool:
    """Whether the outbox path is turned on (default off — direct webhook)."""
    return (os.environ.get("AIFACTORY_COMPLETION_OUTBOX") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def db_path() -> Path:
    """The outbox DB location (override with ``AIFACTORY_COMPLETION_OUTBOX_DB``)."""
    override = (os.environ.get("AIFACTORY_COMPLETION_OUTBOX_DB") or "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".aifactory" / "completion_outbox.db"


def _connect(path: Path | None = None) -> sqlite3.Connection:
    p = path or db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    # WAL + a busy timeout let the relay read while an enqueue writes without
    # "database is locked"; durability survives a process crash.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(_SCHEMA)
    return conn


def _backoff_seconds(attempts: int) -> float:
    """Delay before the next attempt after ``attempts`` failures (1-based)."""
    if attempts <= 0:
        return 0.0
    return min(_BACKOFF_BASE_S * (2 ** (attempts - 1)), _BACKOFF_CAP_S)


def enqueue(
    event: dict,
    url: str,
    *,
    path: Path | None = None,
    now: float | None = None,
) -> bool:
    """Durably persist a completion event for delivery to ``url``.

    Keyed on the envelope ``id`` (#466), so enqueueing the same event twice is a
    harmless no-op. Returns ``True`` when a new row was written, ``False`` when
    the event was already queued. The row is committed before any network call —
    that commit is the at-least-once guarantee. Best-effort: on any storage error
    we log and return ``False`` rather than break the terminal transition.
    """
    event_id = str(event.get("id") or "").strip()
    if not event_id:
        logger.warning("outbox enqueue skipped: event has no 'id' (needs #466 envelope)")
        return False
    ts = now if now is not None else time.time()
    created_iso = event.get("updated_at") or event.get("time") or ""
    try:
        with _connect(path) as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO completion_outbox
                    (id, url, payload, correlation_key, created_at, next_attempt_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    url,
                    json.dumps(event),
                    event.get("correlation_key"),
                    created_iso,
                    ts,  # due immediately
                ),
            )
            return cur.rowcount > 0
    except sqlite3.Error:
        logger.exception("outbox enqueue failed (best-effort)")
        return False


def _default_sender(url: str, payload: str) -> None:
    """POST ``payload`` to ``url``; raise :class:`DeliveryError` on non-2xx.

    ``urllib`` already raises ``HTTPError`` for 4xx/5xx, so reaching the end means
    a 2xx. Stdlib-only, mirroring the direct emitter's transport.
    """
    import urllib.error
    import urllib.request

    timeout = float(os.environ.get("AIFACTORY_COMPLETION_WEBHOOK_TIMEOUT", "5"))
    req = urllib.request.Request(
        url,
        data=payload.encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            code = resp.getcode()
            resp.read()
    except urllib.error.URLError as exc:  # connection refused, DNS, HTTPError, …
        raise DeliveryError(str(exc)) from exc
    if code is not None and not (200 <= int(code) < 300):
        raise DeliveryError(f"non-2xx response: {code}")


def deliver_due_once(
    *,
    path: Path | None = None,
    now: float | None = None,
    sender=None,
    max_batch: int = 100,
) -> dict:
    """Attempt delivery of every due, undelivered row once.

    A row is *due* when ``delivered_at IS NULL`` and ``next_attempt_at <= now``.
    On a 2xx the row is stamped delivered; on failure ``attempts`` is bumped and
    the row is rescheduled with exponential backoff (or abandoned past
    ``_MAX_ATTEMPTS``). Returns counts for observability. Never raises.
    """
    ts = now if now is not None else time.time()
    send = sender or _default_sender
    delivered = failed = abandoned = 0
    try:
        conn = _connect(path)
    except sqlite3.Error:
        logger.exception("outbox open failed (best-effort)")
        return {"delivered": 0, "failed": 0, "abandoned": 0, "remaining": 0}
    try:
        rows = conn.execute(
            """
            SELECT id, url, payload, attempts FROM completion_outbox
            WHERE delivered_at IS NULL AND next_attempt_at <= ?
            ORDER BY created_at ASC, rowid ASC
            LIMIT ?
            """,
            (ts, max_batch),
        ).fetchall()
        for row in rows:
            try:
                send(row["url"], row["payload"])
            except Exception as exc:  # noqa: BLE001 — transport is best-effort
                attempts = int(row["attempts"]) + 1
                if attempts >= _MAX_ATTEMPTS:
                    abandoned += 1
                    # Leave delivered_at NULL but push next_attempt_at far out so
                    # it stops being picked up; last_error records why.
                    conn.execute(
                        "UPDATE completion_outbox SET attempts=?, "
                        "next_attempt_at=?, last_error=? WHERE id=?",
                        (attempts, ts + _BACKOFF_CAP_S * 24, str(exc), row["id"]),
                    )
                    logger.error(
                        "outbox abandoned event %s after %d attempts: %s",
                        row["id"], attempts, exc,
                    )
                else:
                    failed += 1
                    conn.execute(
                        "UPDATE completion_outbox SET attempts=?, "
                        "next_attempt_at=?, last_error=? WHERE id=?",
                        (attempts, ts + _backoff_seconds(attempts), str(exc), row["id"]),
                    )
                continue
            conn.execute(
                "UPDATE completion_outbox SET delivered_at=?, last_error=NULL WHERE id=?",
                (_iso(ts), row["id"]),
            )
            delivered += 1
        remaining = conn.execute(
            "SELECT COUNT(*) FROM completion_outbox WHERE delivered_at IS NULL"
        ).fetchone()[0]
    finally:
        conn.close()
    if delivered or failed or abandoned:
        logger.info(
            "outbox relay: delivered=%d failed=%d abandoned=%d remaining=%d",
            delivered, failed, abandoned, remaining,
        )
    return {
        "delivered": delivered,
        "failed": failed,
        "abandoned": abandoned,
        "remaining": remaining,
    }


def pending_count(*, path: Path | None = None) -> int:
    """Number of rows not yet delivered (for tests/observability)."""
    try:
        with _connect(path) as conn:
            return int(
                conn.execute(
                    "SELECT COUNT(*) FROM completion_outbox WHERE delivered_at IS NULL"
                ).fetchone()[0]
            )
    except sqlite3.Error:
        return 0


def _iso(ts: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


async def relay_loop(*, interval_s: float = 10.0, path: Path | None = None, stop=None) -> None:
    """Background relay: drain due rows every ``interval_s`` until stopped.

    The blocking SQLite/HTTP work runs in a worker thread so the event loop is
    never blocked. ``stop`` is an ``asyncio.Event``; when set the loop exits at
    the next tick. Best-effort — a delivery error in one tick never kills the
    loop. Started from the web-server lifespan only when the flag is on.
    """
    import asyncio

    stop = stop or asyncio.Event()
    logger.info("completion outbox relay started (interval=%.0fs, db=%s)",
                interval_s, path or db_path())
    while not stop.is_set():
        try:
            await asyncio.to_thread(deliver_due_once, path=path)
        except Exception:  # noqa: BLE001 — never let the relay die
            logger.exception("outbox relay tick failed (best-effort)")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
        except asyncio.TimeoutError:
            pass
    logger.info("completion outbox relay stopped")
