#!/usr/bin/env python3
"""
Regression: a merge-failed parallel subtask is reset to PENDING (no orphaned work)
==================================================================================

Found live: a wave subtask whose merge-back conflicted was left 'completed'
(the coder marked it done in its child worktree) but its files never landed on
the task branch — and the serial loop then skipped it, losing the work and
failing integration. On merge failure the canonical plan subtask must be reset
to PENDING so the serial loop re-implements it.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "backend"))

from agents.parallel_integration import reset_subtask_status_pending  # noqa: E402
from implementation_plan.plan import ImplementationPlan  # noqa: E402


def _write_plan(path: Path) -> None:
    plan = {
        "feature": "gw",
        "workflow_type": "feature",
        "phases": [
            {
                "id": "p1",
                "name": "Modules",
                "subtasks": [
                    {
                        "id": "subtask-1-2",
                        "description": "version",
                        "status": "completed",
                        "files_to_create": ["app/routers/version.py"],
                    },
                    {
                        "id": "subtask-1-3",
                        "description": "upstreams",
                        "status": "completed",
                        "files_to_create": ["app/routers/upstreams.py"],
                    },
                ],
            }
        ],
    }
    path.write_text(json.dumps(plan))


def _status(path: Path, sid: str) -> str:
    p = json.loads(path.read_text())
    for ph in p["phases"]:
        for s in ph["subtasks"]:
            if s["id"] == sid:
                return s["status"]
    return "?"


def test_reset_sets_pending(tmp_path):
    plan_path = tmp_path / "implementation_plan.json"
    _write_plan(plan_path)
    assert _status(plan_path, "subtask-1-3") == "completed"  # orphaned-completed

    assert reset_subtask_status_pending(plan_path, "subtask-1-3") is True

    assert _status(plan_path, "subtask-1-3") == "pending"  # redo-able by serial loop
    assert _status(plan_path, "subtask-1-2") == "completed"  # others untouched


def test_reset_unknown_subtask_returns_false(tmp_path):
    plan_path = tmp_path / "implementation_plan.json"
    _write_plan(plan_path)
    assert reset_subtask_status_pending(plan_path, "nope") is False
    assert _status(plan_path, "subtask-1-3") == "completed"  # unchanged


def test_reset_missing_plan_returns_false(tmp_path):
    assert reset_subtask_status_pending(tmp_path / "absent.json", "x") is False


def test_reset_roundtrips_via_plan_loader(tmp_path):
    # The reset must survive a real ImplementationPlan load/save round-trip.
    plan_path = tmp_path / "implementation_plan.json"
    _write_plan(plan_path)
    reset_subtask_status_pending(plan_path, "subtask-1-3")
    loaded = ImplementationPlan.load(plan_path)
    statuses = {s.id: s.status.value for ph in loaded.phases for s in ph.subtasks}
    assert statuses["subtask-1-3"] == "pending"
    assert statuses["subtask-1-2"] == "completed"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
