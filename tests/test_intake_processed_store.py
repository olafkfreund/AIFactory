"""Tests for the durable processed-store (RFC-0011 #636 guard 1)."""

from __future__ import annotations

from intake import processed_store as ps


def test_mark_processed_is_exactly_once(tmp_path):
    db = tmp_path / "p.db"
    assert ps.mark_processed("o/r", 1, path=db) is True  # first claim wins
    assert ps.mark_processed("o/r", 1, path=db) is False  # duplicate ignored
    assert ps.is_processed("o/r", 1, path=db) is True
    assert ps.processed_count(path=db) == 1


def test_distinct_repos_and_numbers_are_independent(tmp_path):
    db = tmp_path / "p.db"
    assert ps.mark_processed("o/r", 1, path=db) is True
    assert ps.mark_processed("o/r", 2, path=db) is True
    assert ps.mark_processed("other/r", 1, path=db) is True
    assert ps.processed_count(path=db) == 3


def test_persists_across_connections(tmp_path):
    db = tmp_path / "p.db"
    ps.mark_processed("o/r", 7, path=db)
    # New connection (simulated restart) sees the durable row.
    assert ps.is_processed("o/r", 7, path=db) is True
    assert ps.mark_processed("o/r", 7, path=db) is False


def test_unmark_allows_reclaim(tmp_path):
    db = tmp_path / "p.db"
    ps.mark_processed("o/r", 3, path=db)
    ps.unmark_processed("o/r", 3, path=db)
    assert ps.is_processed("o/r", 3, path=db) is False
    assert ps.mark_processed("o/r", 3, path=db) is True  # reclaimable after rollback


# --- #870: two-phase claim (confirm) + reclaim of a crashed (stale) claim ---


def test_fresh_claim_is_unconfirmed_and_not_reclaimable_until_stale(tmp_path):
    db = tmp_path / "p.db"
    assert ps.mark_processed("o/r", 1, path=db) is True  # fresh claim
    # A fresh (not yet stale) unconfirmed claim is NOT reclaimable.
    assert ps.mark_processed("o/r", 1, reclaim_after_s=600, path=db) is False


def test_stale_unconfirmed_claim_is_reclaimed(tmp_path):
    """A crashed route (claimed, never confirmed) is reclaimable once stale, so
    the issue is routed again instead of being stranded forever (#870)."""
    db = tmp_path / "p.db"
    assert ps.mark_processed("o/r", 2, path=db) is True
    # reclaim_after_s=0 -> the claim counts as stale immediately.
    assert ps.mark_processed("o/r", 2, reclaim_after_s=0, path=db) is True, (
        "a stale unconfirmed claim must be reclaimable"
    )


def test_confirmed_claim_is_never_reclaimed(tmp_path):
    db = tmp_path / "p.db"
    assert ps.mark_processed("o/r", 3, path=db) is True
    ps.confirm_processed("o/r", 3, path=db)
    # Even stale, a confirmed claim is a completed route — never reclaim it.
    assert ps.mark_processed("o/r", 3, reclaim_after_s=0, path=db) is False


def test_migration_defaults_existing_rows_to_confirmed(tmp_path):
    """A DB that predates the confirmed column must treat its rows as confirmed
    on upgrade — never mass-reclaim every previously-handled issue (#870)."""
    import sqlite3

    db = tmp_path / "old.db"
    c = sqlite3.connect(str(db), isolation_level=None)
    c.execute(
        "CREATE TABLE intake_processed (repo TEXT, issue_number INTEGER, tier TEXT, "
        "routed_to TEXT, processed_at TEXT, PRIMARY KEY(repo, issue_number))"
    )
    c.execute(
        "INSERT INTO intake_processed VALUES "
        "('o/r', 9, 'low', 'aifactory', '2020-01-01T00:00:00+00:00')"
    )
    c.close()
    # Opening via the store migrates the schema; the ancient row is stale but
    # must be confirmed (default 1), so it is NOT reclaimed.
    assert ps.is_processed("o/r", 9, path=db) is True
    assert ps.mark_processed("o/r", 9, reclaim_after_s=0, path=db) is False
