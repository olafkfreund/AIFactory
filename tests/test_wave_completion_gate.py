#!/usr/bin/env python3
"""The parallel wave path must pass the same honesty gates as the serial coder (#1177).

The three gates (#851 test evidence, #1111 deliverable coverage, and #1113 once
it lands) were wired into ``apply_subtask_status_update`` — the tool the SERIAL
coder calls. A wave child never calls it: it is completed by the orchestrator
via ``record_subtask_completion``, so a subtask the serial path would refuse
completed silently on the wave path. Waves are the path used for the larger
work, which is where an unverified completion is most expensive.

``test_the_bypass`` documents the hole with the exact call the wave path used to
make; every test after it drives the REAL wave orchestrator
(:func:`agents.parallel_runner.run_parallel_phase`) with the REAL completion
function (:func:`agents.parallel_integration.gated_mark_complete`), only the
agent session and the git merge faked — those are the two steps that need a
model and a repo, and neither decides whether a subtask is "completed".
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "backend"))

from agents.parallel_integration import gated_mark_complete  # noqa: E402
from agents.parallel_runner import SubtaskResult, run_parallel_phase  # noqa: E402
from agents.utils import record_subtask_completion  # noqa: E402
from implementation_plan.enums import SubtaskStatus  # noqa: E402
from implementation_plan.phase import Phase  # noqa: E402
from implementation_plan.plan import ImplementationPlan  # noqa: E402
from implementation_plan.subtask import Subtask  # noqa: E402

# The live #1111 shape: wave worker C2 wrote a correct router, never registered
# it in app.main, and tested it through a FastAPI() built inside its own test
# file. Genuinely green; POST /api/quote a 404 on the shipped service.
HOLLOW_TEST = (
    "from fastapi import FastAPI\n"
    "from fastapi.testclient import TestClient\n"
    "from app.vat_quote import router\n"
    "_app = FastAPI()\n"
    "_app.include_router(router)\n"
    "client = TestClient(_app)\n"
    "def test_quote():\n"
    "    assert client.post('/api/quote').status_code == 200\n"
)
HONEST_TEST = (
    "from fastapi.testclient import TestClient\n"
    "from app.main import app\n"
    "client = TestClient(app)\n"
    "def test_quote():\n"
    "    assert client.post('/api/quote').status_code == 200\n"
)

RAN_GREEN: dict[str, Any] = {
    "ran": True,
    "last_failed": False,
    "runs": 1,
    "last_command": "pytest -q",
}
NEVER_RAN: dict[str, Any] = {
    "ran": False,
    "last_failed": False,
    "runs": 0,
    "last_command": None,
}


def _project(tmp_path: Path, test_source: str | None) -> Path:
    """A repo whose only test naming /api/quote is `test_source` (or no test)."""
    root = tmp_path / "project"
    (root / "tests").mkdir(parents=True)
    if test_source:
        (root / "tests" / "test_vat_quote.py").write_text(test_source)
    return root


def _plan_file(tmp_path: Path, subtasks: list[Subtask]) -> Path:
    spec = tmp_path / "spec"
    spec.mkdir(exist_ok=True)
    path = spec / "implementation_plan.json"
    ImplementationPlan(
        feature="vat quote endpoint",
        phases=[Phase(phase=1, name="Implementation", subtasks=subtasks)],
    ).save(path)
    return path


def _status(path: Path, sid: str) -> SubtaskStatus | None:
    for ph in ImplementationPlan.load(path).phases:
        for st in ph.subtasks:
            if st.id == sid:
                return st.status
    return None


async def _run_wave(
    subtasks: list[Subtask],
    plan_file: Path,
    project_dir: Path,
    evidence: dict[str, dict[str, Any]],
    *,
    gated: bool = True,
):
    """Drive the real orchestrator; fake only the agent session and the merge."""

    async def run_subtask(subtask: Any, *, index: int) -> SubtaskResult:  # noqa: ARG001
        return SubtaskResult(subtask_id=subtask.id, success=True, worktree_name="wt")

    async def merge_subtask(subtask: Any, result: SubtaskResult) -> bool:  # noqa: ARG001
        return True

    async def ungated_mark_complete(subtask: Any) -> None:
        """The wave path BEFORE #1177: complete on 'session ok + merge ok'."""
        record_subtask_completion(subtask.id, plan_file, None)

    async def mark_complete(subtask: Any) -> bool:
        return await gated_mark_complete(
            subtask,
            plan_path=plan_file,
            source_spec_dir=None,
            project_dir=project_dir,
            evidence=evidence.get(subtask.id, RAN_GREEN),
        )

    return await run_parallel_phase(
        subtasks,
        workers=2,
        run_subtask=run_subtask,
        merge_subtask=merge_subtask,
        mark_complete=mark_complete if gated else ungated_mark_complete,
    )


# ── the bypass, demonstrated ─────────────────────────────────────────────────


async def test_the_bypass(tmp_path):
    """Pre-#1177 wave completion: the C2 shape completes green, ungated.

    The serial path refuses this exact subtask (#1111). Reproduced here with the
    call the wave path actually made, so the hole is a fact in the repo and not
    a claim in a commit message.
    """
    project = _project(tmp_path, HOLLOW_TEST)
    c2 = Subtask(id="C2", description="Add POST /api/quote endpoint")
    plan_file = _plan_file(tmp_path, [c2])

    result = await _run_wave([c2], plan_file, project, {}, gated=False)

    assert result.completed_ids == ["C2"]
    assert _status(plan_file, "C2") == SubtaskStatus.COMPLETED

    # ...and the serial path's gate, on the same subtask and the same tree,
    # would have refused it.
    from agents.completion_gate import completion_refusal

    refusal = completion_refusal(c2.to_dict(), project, RAN_GREEN)
    assert refusal and "never touch the shipped application" in refusal


# ── the bypass, closed ───────────────────────────────────────────────────────


async def test_wave_child_refused_when_tests_never_touch_the_shipped_app(tmp_path):
    """#1111 on the wave path: refused, not completed, and queued for a redo."""
    project = _project(tmp_path, HOLLOW_TEST)
    c2 = Subtask(id="C2", description="Add POST /api/quote endpoint")
    plan_file = _plan_file(tmp_path, [c2])

    result = await _run_wave([c2], plan_file, project, {})

    assert result.completed_ids == []
    assert result.failed_ids == ["C2"]
    assert _status(plan_file, "C2") != SubtaskStatus.COMPLETED


async def test_wave_child_refused_when_no_test_command_ran(tmp_path):
    """#851 on the wave path, read from the CHILD's evidence, not the parent's."""
    project = _project(tmp_path, None)
    t1 = Subtask(id="T1", description="Run all tests for the quote module")
    plan_file = _plan_file(tmp_path, [t1])

    result = await _run_wave([t1], plan_file, project, {"T1": NEVER_RAN})

    assert result.failed_ids == ["T1"]
    assert _status(plan_file, "T1") != SubtaskStatus.COMPLETED


async def test_wave_child_refused_over_a_failing_test_run(tmp_path):
    project = _project(tmp_path, None)
    t1 = Subtask(id="T1", description="Run the unit tests")
    plan_file = _plan_file(tmp_path, [t1])
    failed = {**RAN_GREEN, "last_failed": True}

    result = await _run_wave([t1], plan_file, project, {"T1": failed})

    assert result.failed_ids == ["T1"]
    assert _status(plan_file, "T1") != SubtaskStatus.COMPLETED


# ── and legitimate work still completes ──────────────────────────────────────


async def test_honest_wave_child_completes(tmp_path):
    """A gate that blocks every wave is not a fix: honest work must pass."""
    project = _project(tmp_path, HONEST_TEST)
    c2 = Subtask(id="C2", description="Add POST /api/quote endpoint")
    plan_file = _plan_file(tmp_path, [c2])

    result = await _run_wave([c2], plan_file, project, {})

    assert result.ok
    assert result.completed_ids == ["C2"]
    assert _status(plan_file, "C2") == SubtaskStatus.COMPLETED


async def test_a_refusal_does_not_take_the_wave_down_with_it(tmp_path):
    """Blast radius: siblings that ARE proven still complete in the same wave."""
    project = _project(tmp_path, HOLLOW_TEST)
    c1 = Subtask(id="C1", description="Add slugify() to strutil")
    c2 = Subtask(id="C2", description="Add POST /api/quote endpoint")
    plan_file = _plan_file(tmp_path, [c1, c2])

    result = await _run_wave([c1, c2], plan_file, project, {})

    assert result.completed_ids == ["C1"]
    assert result.failed_ids == ["C2"]
    assert _status(plan_file, "C1") == SubtaskStatus.COMPLETED
    assert _status(plan_file, "C2") != SubtaskStatus.COMPLETED


async def test_escape_hatch_is_the_same_one(tmp_path, monkeypatch):
    """One flag for all three gates, both engines — no wave-only switch."""
    monkeypatch.setenv("AIFACTORY_TEST_EVIDENCE_GATE", "off")
    project = _project(tmp_path, HOLLOW_TEST)
    c2 = Subtask(id="C2", description="Add POST /api/quote endpoint")
    plan_file = _plan_file(tmp_path, [c2])

    result = await _run_wave([c2], plan_file, project, {})

    assert result.completed_ids == ["C2"]


# ── the seam itself ──────────────────────────────────────────────────────────


async def test_both_engines_call_the_same_gate(tmp_path, monkeypatch):
    """Structural: neither engine may hold its own copy of the checks.

    Patching the shared gate has to change BOTH the serial tool and the wave
    path — that is what stops a fourth gate landing on one path only.
    """
    import agents.completion_gate as gate_mod
    import agents.parallel_integration as wave_mod
    import agents.tools_pkg.tools.subtask as serial_mod

    calls: list[str] = []

    def _spy(subtask, project_dir, evidence):  # noqa: ARG001
        calls.append(str(subtask.get("id")))
        return "Refused: spy"

    monkeypatch.setattr(gate_mod, "completion_refusal", _spy)
    monkeypatch.setattr(wave_mod, "completion_refusal", _spy)

    project = _project(tmp_path, None)
    st = Subtask(id="S1", description="Add slugify()")
    plan_file = _plan_file(tmp_path, [st])

    # wave engine
    assert (
        await gated_mark_complete(
            st,
            plan_path=plan_file,
            source_spec_dir=None,
            project_dir=project,
            evidence=RAN_GREEN,
        )
        is False
    )
    # serial engine (imports the gate at call time, so the patch above lands)
    out = await serial_mod.apply_subtask_status_update(
        plan_file.parent, "S1", "completed", project_dir=project
    )
    assert "Refused: spy" in out["content"][0]["text"]
    assert calls == ["S1", "S1"]
    assert _status(plan_file, "S1") != SubtaskStatus.COMPLETED


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
