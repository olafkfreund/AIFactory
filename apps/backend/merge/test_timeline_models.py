"""Tests for TaskFileView.from_dict status recovery (Factory#431).

A persisted TaskFileView with a missing or unrecognised ``status`` field used
to silently become "active" -- every caller that filters on
``status == "active"`` (drift tracking, pending-task queries, the tracker
CLI's active count) would then treat a corrupt/stale record as an ongoing
task. The fix reports "unknown" instead, so a caller sees "we don't know",
not an invented "still working".
"""

from __future__ import annotations

import logging

from merge.timeline_models import TaskFileView

_BRANCH_POINT = {
    "commit_hash": "abc123",
    "content": "hello",
    "timestamp": "2026-01-01T00:00:00",
}


def _data(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {"task_id": "task-1", "branch_point": _BRANCH_POINT}
    data.update(overrides)
    return data


def test_missing_status_becomes_unknown_not_active(caplog) -> None:
    """No 'status' key at all -- e.g. a pre-migration persisted record."""
    with caplog.at_level(logging.WARNING):
        view = TaskFileView.from_dict(_data())
    assert view.status == "unknown"
    assert "task-1" in caplog.text


def test_unrecognised_status_becomes_unknown_not_active(caplog) -> None:
    """A corrupt or foreign status string must not be trusted verbatim."""
    with caplog.at_level(logging.WARNING):
        view = TaskFileView.from_dict(_data(status="corrupted-garbage"))
    assert view.status == "unknown"
    assert "corrupted-garbage" in caplog.text


def test_known_statuses_pass_through_unchanged() -> None:
    for status in ("active", "merged", "abandoned"):
        view = TaskFileView.from_dict(_data(status=status))
        assert view.status == status


def test_unknown_status_is_excluded_from_active_filtering() -> None:
    """The whole point of "unknown" over "active": callers that gate on
    status == "active" (get_active_tasks, drift tracking) must not act on a
    record whose real status nobody knows.
    """
    view = TaskFileView.from_dict(_data())
    assert view.status != "active"
