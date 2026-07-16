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
    "confirm_processed",
    "db_path",
    "is_processed",
    "mark_processed",
    "processed_count",
    "unmark_processed",
]

# #870: seconds a claim may sit UNCONFIRMED before it is treated as a crashed
# route and made reclaimable. Must comfortably exceed a normal route+dispatch
# (which confirms within seconds), so only a genuinely stranded claim — the pod
# died between claiming and confirming — is ever reclaimed. Override for tests.
RECLAIM_AFTER_S = 600.0

# ``confirmed`` (#870): the two-phase claim. A row is inserted UNCONFIRMED (0);
# ``confirm_processed`` sets it to 1 once the issue has actually been routed. A
# crash between the two leaves an unconfirmed row that ``mark_processed`` can
# reclaim after RECLAIM_AFTER_S. The column DEFAULTs to 1 so that (a) rows that
# predate this feature are treated as confirmed (never mass-reclaimed on
# upgrade) and (b) a plain INSERT that forgets to set it is safe; new claims set
# it to 0 explicitly.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS intake_processed (
    repo         TEXT NOT NULL,
    issue_number INTEGER NOT NULL,
    tier         TEXT,
    routed_to    TEXT,
    processed_at TEXT NOT NULL,
    confirmed    INTEGER NOT NULL DEFAULT 1,
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
    # Migrate an existing table that predates the ``confirmed`` column (#870).
    # ADD COLUMN backfills existing rows with the DEFAULT (1 = confirmed), so
    # already-claimed issues are never mass-reclaimed on upgrade.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(intake_processed)")}
    if "confirmed" not in cols:
        conn.execute(
            "ALTER TABLE intake_processed "
            "ADD COLUMN confirmed INTEGER NOT NULL DEFAULT 1"
        )
    return conn


def _now() -> float:
    import time

    return time.time()


def _iso(ts: float | None = None) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(
        ts if ts is not None else _now(), tz=timezone.utc
    ).isoformat()


def mark_processed(  # noqa: PLR0913 — all keyword-only; a claim needs its coordinates
    repo: str,
    issue_number: int,
    *,
    tier: str | None = None,
    routed_to: str | None = None,
    reclaim_after_s: float = RECLAIM_AFTER_S,
    path: Path | None = None,
) -> bool:
    """Atomically claim ``(repo, issue_number)`` as UNCONFIRMED (#870).

    Returns ``True`` when this call won the claim (the caller should route the
    issue), ``False`` when it was already claimed by a live/confirmed row.

    Two-phase (#870): a fresh claim is inserted ``confirmed=0``; the caller must
    call :func:`confirm_processed` once the issue is actually routed. If a prior
    claim is still ``confirmed=0`` and older than ``reclaim_after_s`` — i.e. the
    process died between claiming and confirming and left the issue stranded —
    this RECLAIMS it (a single conditional UPDATE, so across concurrent pollers
    exactly one wins) and returns ``True`` so it is routed again. A confirmed or
    still-fresh claim is left alone and returns ``False``.

    Best-effort on storage error: returns ``False`` so the issue is treated as
    already-handled rather than double-dispatched.
    """
    try:
        with _connect(path) as conn:
            now = _iso()
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO intake_processed
                    (repo, issue_number, tier, routed_to, processed_at, confirmed)
                VALUES (?, ?, ?, ?, ?, 0)
                """,
                (repo, int(issue_number), tier, routed_to, now),
            )
            if cur.rowcount > 0:
                return True  # fresh claim
            # Row exists — reclaim it only if it is an unconfirmed, stale claim
            # (a crashed route). The WHERE clause is the whole guard: a confirmed
            # row or a fresh unconfirmed one never matches, and once one poller's
            # UPDATE stamps a new processed_at another poller's identical UPDATE
            # no longer matches the age predicate — so the reclaim is atomic.
            cutoff = _iso(_now() - float(reclaim_after_s))
            cur = conn.execute(
                """
                UPDATE intake_processed
                   SET processed_at=?, tier=?, routed_to=?
                 WHERE repo=? AND issue_number=?
                   AND confirmed=0 AND processed_at < ?
                """,
                (now, tier, routed_to, repo, int(issue_number), cutoff),
            )
            return cur.rowcount > 0
    except sqlite3.Error:
        logger.exception("intake processed-store write failed (best-effort)")
        return False


def confirm_processed(
    repo: str, issue_number: int, *, path: Path | None = None
) -> None:
    """Mark a claim CONFIRMED (#870) — called once the issue is actually routed.

    A confirmed claim is never reclaimed, so this closes the crash window: an
    unconfirmed claim means the route did not complete and may be retried; a
    confirmed one means it did. Best-effort: storage errors are swallowed.
    """
    try:
        with _connect(path) as conn:
            conn.execute(
                "UPDATE intake_processed SET confirmed=1 "
                "WHERE repo=? AND issue_number=?",
                (repo, int(issue_number)),
            )
    except sqlite3.Error:
        logger.exception("intake processed-store confirm failed (best-effort)")


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
