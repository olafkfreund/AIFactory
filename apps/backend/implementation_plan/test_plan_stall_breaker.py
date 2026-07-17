"""Stall-breaker for get_next_subtask (#896).

A planner slip that gives the entry phase an unsatisfiable dependency (forward
or cyclic phase dep) must not hard-fail the build "0/N no runnable subtask" —
get_next_subtask falls back to the earliest incomplete phase with pending work.
"""

from implementation_plan.enums import SubtaskStatus
from implementation_plan.phase import Phase
from implementation_plan.plan import ImplementationPlan
from implementation_plan.subtask import Subtask


def _st(i: str) -> Subtask:
    return Subtask(id=i, description=i)


def test_stall_breaker_runs_earliest_phase_when_deps_unsatisfiable() -> None:
    # Every phase depends on a later phase -> no phase is ever "available" at
    # start, so the old code returned None -> build hard-failed 0/N.
    p1 = Phase(phase=1, name="one", subtasks=[_st("a")], depends_on=[2])
    p2 = Phase(phase=2, name="two", subtasks=[_st("b")], depends_on=[3])
    plan = ImplementationPlan(feature="f", phases=[p1, p2])

    nxt = plan.get_next_subtask()
    assert nxt is not None
    phase, sub = nxt
    assert phase.phase == 1 and sub.id == "a"


def test_no_false_fire_when_plan_complete() -> None:
    s = _st("a")
    s.status = SubtaskStatus.COMPLETED
    plan = ImplementationPlan(
        feature="f", phases=[Phase(phase=1, name="one", subtasks=[s])]
    )
    assert plan.get_next_subtask() is None


def test_normal_available_phase_still_wins() -> None:
    p1 = Phase(phase=1, name="one", subtasks=[_st("a")])
    plan = ImplementationPlan(feature="f", phases=[p1])
    nxt = plan.get_next_subtask()
    assert nxt is not None
    phase, sub = nxt
    assert phase.phase == 1 and sub.id == "a"
