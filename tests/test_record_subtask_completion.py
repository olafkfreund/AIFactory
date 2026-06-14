"""record_subtask_completion must persist subtask completion even when the
worktree spec dir has no implementation_plan.json.

Regression: the parallel coder records completion via the parent's plan_path
(spec_dir/implementation_plan.json). When that worktree spec dir held no plan,
the load threw, the caller swallowed it, completion was silently lost, the
canonical plan stayed at 0 completed, and a SUCCESSFUL build was reported failed.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "backend"))

from agents.utils import record_subtask_completion  # noqa: E402
from implementation_plan.enums import SubtaskStatus  # noqa: E402
from implementation_plan.phase import Phase  # noqa: E402
from implementation_plan.plan import ImplementationPlan  # noqa: E402
from implementation_plan.subtask import Subtask  # noqa: E402


def _plan(*ids: str) -> ImplementationPlan:
    return ImplementationPlan(
        feature="hello greeting",
        phases=[
            Phase(
                phase=1,
                name="Implementation",
                subtasks=[Subtask(id=i, description=f"do {i}") for i in ids],
            )
        ]
    )


def _status(path: Path, sid: str):
    plan = ImplementationPlan.load(path)
    for ph in plan.phases:
        for st in ph.subtasks:
            if st.id == sid:
                return st.status
    return None


def test_marks_completion_in_spec_dir_plan(tmp_path):
    spec = tmp_path / "spec"
    spec.mkdir()
    pf = spec / "implementation_plan.json"
    _plan("C1", "C2").save(pf)

    assert record_subtask_completion("C1", pf, None) is True
    assert _status(pf, "C1") == SubtaskStatus.COMPLETED
    assert _status(pf, "C2") == SubtaskStatus.PENDING


def test_falls_back_to_source_when_worktree_plan_missing(tmp_path):
    # The regression: the worktree spec dir has NO plan; the canonical source does.
    worktree = tmp_path / "worktree" / "spec"
    worktree.mkdir(parents=True)
    source = tmp_path / "source"
    source.mkdir()
    src_pf = source / "implementation_plan.json"
    _plan("C1").save(src_pf)

    missing = worktree / "implementation_plan.json"  # does NOT exist
    assert record_subtask_completion("C1", missing, source) is True
    assert _status(src_pf, "C1") == SubtaskStatus.COMPLETED


def test_returns_false_when_no_plan_anywhere(tmp_path):
    missing = tmp_path / "nope" / "implementation_plan.json"
    assert record_subtask_completion("C1", missing, tmp_path / "alsonope") is False


def test_returns_false_for_unknown_subtask(tmp_path):
    spec = tmp_path / "spec"
    spec.mkdir()
    pf = spec / "implementation_plan.json"
    _plan("C1").save(pf)

    assert record_subtask_completion("DOES_NOT_EXIST", pf, None) is False
