#!/usr/bin/env python3
"""
Regression: slug-style phase dependencies gate availability correctly
=====================================================================

Found live (benchmark 004): the Integration phase declared
``depends_on: ["phase-2-modules"]`` (a planner slug), but
``get_available_phases`` compared those strings against a set of integer phase
numbers (``{1, 2}``). ``"phase-2-modules" in {1, 2}`` is always False, so the
Integration phase was never "available" — ``get_next_subtask`` returned None and
the executor printed "No pending subtasks found" while a subtask was still
pending. The build stopped one subtask short (integration + QA skipped).

The fix resolves each dependency token (int OR slug) to its phase number before
the completion check.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "backend"))

from implementation_plan.enums import SubtaskStatus  # noqa: E402
from implementation_plan.phase import Phase  # noqa: E402
from implementation_plan.plan import (  # noqa: E402
    ImplementationPlan,
    _resolve_dep_phase_number,
)
from implementation_plan.subtask import Subtask  # noqa: E402


def _sub(sid: str, status: SubtaskStatus) -> Subtask:
    return Subtask(id=sid, description=sid, status=status)


def _plan(integration_status: SubtaskStatus) -> ImplementationPlan:
    """Phases 1 & 2 complete; phase 3 (integration) pending, slug deps."""
    return ImplementationPlan(
        feature="gw",
        phases=[
            Phase(
                phase=1,
                name="Scaffold",
                subtasks=[_sub("subtask-1-1", SubtaskStatus.COMPLETED)],
            ),
            Phase(
                phase=2,
                name="Parallel Module Implementation",
                depends_on=["phase-1-scaffold"],
                subtasks=[_sub("subtask-2-1", SubtaskStatus.COMPLETED)],
            ),
            Phase(
                phase=3,
                name="Integration",
                depends_on=["phase-2-modules"],
                subtasks=[_sub("subtask-3-1", integration_status)],
            ),
        ],
    )


def test_slug_dep_phase_becomes_available_when_deps_complete():
    plan = _plan(SubtaskStatus.PENDING)
    available = [p.phase for p in plan.get_available_phases()]
    assert available == [3]  # was [] before the fix — phase 3 invisible


def test_get_next_subtask_finds_slug_dep_integration_subtask():
    plan = _plan(SubtaskStatus.PENDING)
    nxt = plan.get_next_subtask()
    assert nxt is not None  # was None before the fix → "No pending subtasks found"
    phase, subtask = nxt
    assert phase.phase == 3
    assert subtask.id == "subtask-3-1"


def test_incomplete_dependency_keeps_phase_gated():
    # If phase 2 is NOT complete, phase 3 must stay unavailable.
    plan = _plan(SubtaskStatus.PENDING)
    plan.phases[1].subtasks[0].status = SubtaskStatus.PENDING  # phase 2 now incomplete
    available = [p.phase for p in plan.get_available_phases()]
    assert 3 not in available
    assert 2 in available  # phase 2 itself is available (its dep, phase 1, is done)


def test_resolver_handles_int_str_and_slug():
    assert _resolve_dep_phase_number(2) == 2
    assert _resolve_dep_phase_number("2") == 2
    assert _resolve_dep_phase_number("phase-2-modules") == 2
    assert _resolve_dep_phase_number("phase-10-foo") == 10
    assert _resolve_dep_phase_number("no-number") is None
    assert _resolve_dep_phase_number(True) is None  # bool guard


def test_integer_deps_still_work():
    # Backward compatibility: integer depends_on must still gate correctly.
    plan = ImplementationPlan(
        feature="gw",
        phases=[
            Phase(phase=1, name="A", subtasks=[_sub("s1", SubtaskStatus.COMPLETED)]),
            Phase(
                phase=2,
                name="B",
                depends_on=[1],
                subtasks=[_sub("s2", SubtaskStatus.PENDING)],
            ),
        ],
    )
    assert [p.phase for p in plan.get_available_phases()] == [2]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
