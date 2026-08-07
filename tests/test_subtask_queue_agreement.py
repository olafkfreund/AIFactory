#!/usr/bin/env python3
"""One queue: the accounting layer and BOTH coding engines must agree (#1176).

``Subtask.is_handoff`` declared ``testing``/``cicd`` subtasks out of the coder's
scope. Three layers read that plan and only one obeyed:

* ``Phase.get_pending_subtasks`` / ``Phase.is_complete`` /
  ``ImplementationPlan.get_next_subtask`` / ``get_progress`` excluded them;
* the serial coder loop (``core.progress.get_next_subtask``, called by
  ``agents.coder``) had no concept of the field and handed them over;
* the wave engine (``agents.parallel_integration``) passes ``phase.subtasks``
  raw to :func:`agents.parallel_runner.run_parallel_phase` and dispatched them
  too.

So the plan reported the build complete with nothing left to do while both
engines were still giving the coder the work. The field is gone: every subtask
in the plan is the coder's. Nothing downstream ever consumed the excluded ones —
``pfactory.tfactory_client.build_handoff_payload`` and ``build_ingest_payload``
carry no subtasks at all (pinned below), and TFactory verifies a build rather
than writing the target repo's pipeline.

This module is the anti-drift seam. It drives all three layers against ONE plan
and asserts they select the same queue, so a future "skip this class of subtask"
rule cannot land on one layer only — which is exactly how this stayed invisible.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "backend"))

from core.progress import get_next_subtask as serial_next_subtask  # noqa: E402
from implementation_plan.plan import ImplementationPlan  # noqa: E402
from implementation_plan.subtask import Subtask  # noqa: E402

# The live #1113 shape: PFactory's decomposition tags its CI/CD and testing
# children via `service`, which is what `is_handoff` keyed on.
PLAN: dict[str, Any] = {
    "task_id": "demo",
    "feature": "vat quote",
    "phases": [
        {
            "phase": 1,
            "name": "Implementation",
            "type": "implementation",
            "parallel_safe": True,
            "subtasks": [
                {
                    "id": "S1",
                    "description": "Add the VAT quote endpoint",
                    "status": "completed",
                    "service": "backend",
                },
                {
                    "id": "CICD",
                    "description": "Set up CI/CD for the VAT quote endpoint",
                    "status": "pending",
                    "service": "cicd",
                    "files_to_modify": [".github/workflows/ci.yml"],
                },
                {
                    "id": "TEST",
                    "description": "Test the VAT quote endpoint",
                    "status": "pending",
                    "service": "testing",
                    "files_to_create": ["tests/test_vat_quote.py"],
                },
            ],
        }
    ],
}


@pytest.fixture
def spec_dir(tmp_path: Path) -> Path:
    d = tmp_path / "spec"
    d.mkdir()
    (d / "implementation_plan.json").write_text(json.dumps(PLAN))
    return d


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    """A real git repo — the wave path reads the task branch off it."""
    root = tmp_path / "project"
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    (root / "README.md").write_text("x\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "init"],
        cwd=root,
        check=True,
    )
    return root


async def _wave_dispatched(
    spec_dir: Path, project_dir: Path, plan: ImplementationPlan, monkeypatch
) -> list[str]:
    """Subtask ids the REAL wave dispatch site hands the orchestrator.

    Only :func:`agents.parallel_runner.run_parallel_phase` is replaced — the
    scheduling loop, which needs agent sessions and git merges. Everything that
    decides WHAT is dispatched is the production code path.
    """
    import agents.parallel_integration as wave_mod
    from agents.parallel_runner import PhaseRunResult

    seen: list[str] = []

    async def _spy(subtasks: list[Any], **kwargs: Any) -> PhaseRunResult:
        seen.extend(s.id for s in subtasks)
        return PhaseRunResult()

    monkeypatch.setattr(wave_mod, "run_parallel_phase", _spy)
    await wave_mod.run_parallel_coding_phase(
        plan=plan,
        phase=plan.phases[0],
        project_dir=project_dir,
        spec_dir=spec_dir,
        model="sonnet",
        workers=4,
    )
    return seen


@pytest.mark.asyncio
async def test_both_engines_and_the_accounting_agree_on_the_queue(
    spec_dir: Path, project_dir: Path, monkeypatch
):
    """The divergence itself. With `is_handoff` honoured by the accounting layer
    only, this asserted: pending == [], is_complete True, next None -- while the
    serial loader returned CICD and the wave dispatched CICD and TEST."""
    plan = ImplementationPlan.load(spec_dir / "implementation_plan.json")
    phase = plan.phases[0]

    accounting = {s.id for s in phase.get_pending_subtasks()}
    assert accounting == {"CICD", "TEST"}
    assert not phase.is_complete()
    assert plan.get_progress()["is_complete"] is False
    nxt = plan.get_next_subtask()
    assert nxt is not None and nxt[1].id in accounting

    serial = serial_next_subtask(spec_dir)
    assert serial is not None
    assert serial["id"] in accounting

    dispatched = await _wave_dispatched(spec_dir, project_dir, plan, monkeypatch)
    # The wave path is handed the phase's full list on purpose: run_parallel_phase
    # seeds its completed set from it so `depends_on` on a finished sibling
    # resolves. What must agree is the PENDING set it will actually run.
    wave_pending = {
        i for i in dispatched if i in {s["id"] for s in PLAN["phases"][0]["subtasks"]}
    } - {"S1"}
    assert wave_pending == accounting


def test_the_serial_loader_and_the_plan_model_select_the_same_subtask(
    spec_dir: Path,
):
    """core.progress is a separate raw-dict loader from ImplementationPlan, so
    the two can drift silently. They must name the same next subtask."""
    plan = ImplementationPlan.load(spec_dir / "implementation_plan.json")
    nxt = plan.get_next_subtask()
    serial = serial_next_subtask(spec_dir)
    assert nxt is not None and serial is not None
    assert serial["id"] == nxt[1].id


def test_a_cicd_subtask_is_ordinary_coder_work(spec_dir: Path):
    """No class of subtask is exempt. `service` is scoping metadata; it does not
    decide whether the coder owns the work."""
    plan = ImplementationPlan.load(spec_dir / "implementation_plan.json")
    cicd = next(s for s in plan.phases[0].subtasks if s.id == "CICD")
    assert cicd.service == "cicd"
    assert not hasattr(cicd, "is_handoff")
    assert cicd in plan.phases[0].get_pending_subtasks()


def test_service_survives_the_dict_round_trip(spec_dir: Path):
    """`service` is what `is_handoff` was derived from and what still tags a
    subtask for the #1113 pipeline gate. ImplementationPlan.save() writes
    to_dict(), so a field it drops is DELETED from implementation_plan.json on
    the first completion -- how acceptance_criteria was being lost (#1175)."""
    st = Subtask(id="CICD", description="Set up CI/CD", service="cicd")
    assert Subtask.from_dict(st.to_dict()).service == "cicd"

    plan_path = spec_dir / "implementation_plan.json"
    ImplementationPlan.load(plan_path).save(plan_path)
    reloaded = ImplementationPlan.load(plan_path)
    assert [s.service for s in reloaded.phases[0].subtasks] == [
        "backend",
        "cicd",
        "testing",
    ]


def test_no_downstream_stage_receives_subtasks(tmp_path: Path):
    """The premise `is_handoff` rested on, checked rather than assumed: neither
    TFactory payload carries the plan's subtasks, so a subtask the coder skipped
    was handed to nobody."""
    from pfactory.tfactory_client import build_handoff_payload, build_ingest_payload

    class _Classification:
        handoff = "tfactory"
        types = ("feature",)
        priority = "high"

    handoff = build_handoff_payload(
        "101-demo", {"title": "t", "description": "d"}, _Classification(), {}
    )
    spec = tmp_path / "spec"
    spec.mkdir()
    (spec / "spec.md").write_text("## Acceptance Criteria\n- it works\n")
    ingest = build_ingest_payload(spec, "101-demo")

    assert not [k for k in (*handoff, *ingest) if "subtask" in k.lower()]


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
