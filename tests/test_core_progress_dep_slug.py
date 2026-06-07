#!/usr/bin/env python3
"""
Regression: core.progress.get_next_subtask resolves slug phase dependencies
===========================================================================

This is the function the live executor (agents/coder.py) actually calls. Found
in benchmark 004: the planner emitted phase dependencies as slugs
("phase-2-modules"), but completion was tracked in a dict keyed by integer phase
number. ``phase_complete.get("phase-2-modules", False)`` was always False, so the
Integration phase never became available — get_next_subtask returned None while
subtask-3-1 was still pending, the coder printed "No pending subtasks found", and
the build stopped one subtask short (integration + QA skipped).
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "backend"))

from core.progress import get_next_subtask  # noqa: E402


def _write(spec_dir: Path, integration_status: str) -> None:
    plan = {
        "feature": "gw",
        "phases": [
            {"phase": 1, "name": "Scaffold", "subtasks": [
                {"id": "subtask-1-1", "description": "scaffold", "status": "completed"}]},
            {"phase": 2, "name": "Parallel Module Implementation",
             "depends_on": ["phase-1-scaffold"], "subtasks": [
                {"id": "subtask-2-1", "description": "mod", "status": "completed"}]},
            {"phase": 3, "name": "Integration", "depends_on": ["phase-2-modules"],
             "subtasks": [
                {"id": "subtask-3-1", "description": "wire", "status": integration_status}]},
        ],
    }
    (spec_dir / "implementation_plan.json").write_text(json.dumps(plan))


def test_slug_dep_integration_subtask_is_found(tmp_path):
    _write(tmp_path, "pending")
    nxt = get_next_subtask(tmp_path)
    assert nxt is not None  # was None before the fix
    assert nxt["id"] == "subtask-3-1"
    assert nxt["phase_name"] == "Integration"


def test_all_complete_returns_none(tmp_path):
    _write(tmp_path, "completed")
    assert get_next_subtask(tmp_path) is None


def test_incomplete_dependency_blocks_integration(tmp_path):
    # Phase 2 incomplete → integration must NOT be selected; phase 2 work returned.
    plan = {
        "feature": "gw",
        "phases": [
            {"phase": 1, "name": "Scaffold", "subtasks": [
                {"id": "subtask-1-1", "description": "s", "status": "completed"}]},
            {"phase": 2, "name": "Modules", "depends_on": ["phase-1-scaffold"],
             "subtasks": [
                {"id": "subtask-2-1", "description": "m", "status": "pending"}]},
            {"phase": 3, "name": "Integration", "depends_on": ["phase-2-modules"],
             "subtasks": [
                {"id": "subtask-3-1", "description": "i", "status": "pending"}]},
        ],
    }
    (tmp_path / "implementation_plan.json").write_text(json.dumps(plan))
    nxt = get_next_subtask(tmp_path)
    assert nxt is not None and nxt["id"] == "subtask-2-1"  # not integration


def test_integer_depends_on_still_works(tmp_path):
    plan = {
        "feature": "gw",
        "phases": [
            {"phase": 1, "name": "A", "subtasks": [
                {"id": "s1", "description": "a", "status": "completed"}]},
            {"phase": 2, "name": "B", "depends_on": [1], "subtasks": [
                {"id": "s2", "description": "b", "status": "pending"}]},
        ],
    }
    (tmp_path / "implementation_plan.json").write_text(json.dumps(plan))
    nxt = get_next_subtask(tmp_path)
    assert nxt is not None and nxt["id"] == "s2"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
