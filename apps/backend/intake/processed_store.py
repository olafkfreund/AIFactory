"""Durable processed-table for RFC-0011 intake idempotency (#636).

The intake poller must process each labelled issue **exactly once**, even across
process restarts. This is the first of the poller's two idempotency guards: a
durable SQLite table keyed on ``(repo, issue_number)``. ``mark_processed`` uses
``INSERT OR IGNORE`` (modeled on the completion outbox) so a row is claimed
atomically — it returns ``True`` only for the caller that wins the insert, which
is the signal to route the issue downstream exactly once.

The second guard (an applied ``factory:queued`` label, checked by the poller's
fetch filter) is independent: even if this DB is deleted, an already-queued issue
is excluded by the label, so a wiped DB cannot cause a double-dispatch.

Stdlib-only, no migration framework — same ethos as ``services/outbox.py``.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = [
    "db_path",
    "is_processed",
    "mark_processed",
    "processed_count",
    "unmark_processed",
]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS intake_processed (
    repo         TEXT NOT NULL,
    issue_number INTEGER NOT NULL,
    tier         TEXT,
    routed_to    TEXT,
    processed_at TEXT NOT NULL,
    PRIMARY KEY (repo, issue_number)
);
"""


def db_path() -> Path:
    """The processed-table DB location (override with ``AIFACTORY_INTAKE_DB``)."""
    override = (os.environ.get("AIFACTORY_INTAKE_DB") or "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".aifactory" / "intake_processed.db"


def _connect(path: Path | None = None) -> sqlite3.Connection:
    p = path or db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(_SCHEMA)
    return conn


def _iso(ts: float | None = None) -> str:
    import time
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ts or time.time(), tz=timezone.utc).isoformat()


def mark_processed(
    repo: str,
    issue_number: int,
    *,
    tier: str | None = None,
    routed_to: str | None = None,
    path: Path | None = None,
) -> bool:
    """Atomically claim ``(repo, issue_number)``.

    Returns ``True`` when this call inserted the row (the caller should route the
    issue), ``False`` when it was already present (a previous run/tick handled it,
    or a concurrent caller won). Best-effort on storage error: returns ``False``
    so the issue is treated as already-handled rather than double-dispatched.
    """
    try:
        with _connect(path) as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO intake_processed
                    (repo, issue_number, tier, routed_to, processed_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (repo, int(issue_number), tier, routed_to, _iso()),
            )
            return cur.rowcount > 0
    except sqlite3.Error:
        logger.exception("intake processed-store write failed (best-effort)")
        return False


def unmark_processed(repo: str, issue_number: int, *, path: Path | None = None) -> None:
    """Release a claim on ``(repo, issue_number)`` (transient-failure rollback).

    Lets the next poll tick retry an issue whose routing failed transiently
    (the ``factory:queued`` label is only applied on success, so removing the
    claim re-opens the issue for a retry). Best-effort: storage errors are
    swallowed.
    """
    try:
        with _connect(path) as conn:
            conn.execute(
                "DELETE FROM intake_processed WHERE repo=? AND issue_number=?",
                (repo, int(issue_number)),
            )
    except sqlite3.Error:
        logger.exception("intake processed-store rollback failed (best-effort)")


def is_processed(repo: str, issue_number: int, *, path: Path | None = None) -> bool:
    """Whether ``(repo, issue_number)`` has already been claimed."""
    try:
        with _connect(path) as conn:
            row = conn.execute(
                "SELECT 1 FROM intake_processed WHERE repo=? AND issue_number=?",
                (repo, int(issue_number)),
            ).fetchone()
            return row is not None
    except sqlite3.Error:
        return False


def processed_count(*, path: Path | None = None) -> int:
    """Total processed rows (tests/observability)."""
    try:
        with _connect(path) as conn:
            return int(
                conn.execute("SELECT COUNT(*) FROM intake_processed").fetchone()[0]
            )
    except sqlite3.Error:
        return 0
