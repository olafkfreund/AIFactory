"""Tests for the silent no-op build guard (#779).

A run that finishes with subtasks planned but ZERO completed must be treated
as a failure (run.py exits 1), so a headless build Job is marked Failed
instead of Complete with an empty branch. Legitimate 0-completed exits
(PAUSE file, plan-review human_review pause) must NOT trip the guard.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cli.build_commands import build_is_silent_noop


def _write_plan(
    spec_dir: Path,
    statuses: list[str],
    plan_status: str | None = None,
) -> None:
    plan: dict[str, Any] = {
        "phases": [
            {"subtasks": [{"id": f"s{i}", "status": s} for i, s in enumerate(statuses)]}
        ]
    }
    if plan_status is not None:
        plan["status"] = plan_status
    (spec_dir / "implementation_plan.json").write_text(
        json.dumps(plan), encoding="utf-8"
    )


def test_zero_completed_is_noop(tmp_path: Path) -> None:
    _write_plan(tmp_path, ["pending", "pending", "failed"])
    assert build_is_silent_noop(tmp_path) is True


def test_all_failed_is_noop(tmp_path: Path) -> None:
    _write_plan(tmp_path, ["failed", "failed"])
    assert build_is_silent_noop(tmp_path) is True


def test_any_completed_is_not_noop(tmp_path: Path) -> None:
    _write_plan(tmp_path, ["completed", "pending", "failed"])
    assert build_is_silent_noop(tmp_path) is False


def test_no_plan_is_not_noop(tmp_path: Path) -> None:
    assert build_is_silent_noop(tmp_path) is False


def test_empty_plan_is_not_noop(tmp_path: Path) -> None:
    _write_plan(tmp_path, [])
    assert build_is_silent_noop(tmp_path) is False


def test_pause_file_excluded(tmp_path: Path) -> None:
    _write_plan(tmp_path, ["pending", "pending"])
    (tmp_path / "PAUSE").write_text("", encoding="utf-8")
    assert build_is_silent_noop(tmp_path) is False


def test_plan_review_pause_excluded(tmp_path: Path) -> None:
    _write_plan(tmp_path, ["pending", "pending"], plan_status="human_review")
    assert build_is_silent_noop(tmp_path) is False


def test_unreadable_plan_status_still_noop(tmp_path: Path) -> None:
    # count_subtasks reads the same file; a corrupt plan yields total == 0 so
    # the guard never fires — never fail a build on unreadable metadata.
    (tmp_path / "implementation_plan.json").write_text("{not json", encoding="utf-8")
    assert build_is_silent_noop(tmp_path) is False
