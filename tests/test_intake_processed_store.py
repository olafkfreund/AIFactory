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
