"""Tests for PFactory taxonomy → scheduling/routing (epic #327, #331).

Covers the pure decisions in ``pfactory.routing`` plus the two wired behaviours:
priority-ordered task listing and TFactory routing at task start.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from pfactory.routing import CODER, TFACTORY, priority_rank, routing_target
from pfactory.taxonomy import classify_labels

# The wired behaviours live in the web-server package.
_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "apps" / "web-server"))
sys.path.insert(0, str(_ROOT / "apps" / "backend"))


# ── priority_rank (pure) ───────────────────────────────────────────────────


def test_priority_rank_orders_p0_first():
    assert (
        priority_rank("p0")
        < priority_rank("p1")
        < priority_rank("p2")
        < priority_rank("p3")
    )


def test_priority_rank_unknown_sorts_last():
    assert priority_rank(None) == 99
    assert priority_rank("") == 99
    assert priority_rank("urgent") == 99
    assert priority_rank("P0") == priority_rank("p0")  # case-insensitive


# ── routing_target (pure) ──────────────────────────────────────────────────


def test_routing_handoff_tfactory_goes_to_tfactory():
    c = classify_labels(["pfactory", "handoff:tfactory", "type:testing"])
    assert routing_target(c) == TFACTORY


def test_routing_handoff_aifactory_goes_to_coder_even_with_testing():
    # Explicit AIFactory handoff is authoritative over a type:testing label.
    c = classify_labels(["pfactory", "handoff:aifactory", "type:testing"])
    assert routing_target(c) == CODER


def test_routing_type_testing_without_handoff_goes_to_tfactory():
    c = classify_labels(["type:testing"])
    assert routing_target(c) == TFACTORY


def test_routing_plain_work_goes_to_coder():
    c = classify_labels(["pfactory", "handoff:aifactory", "type:software"])
    assert routing_target(c) == CODER
    assert routing_target(classify_labels(["bug"])) == CODER


# ── priority scheduling: list_tasks ordering ───────────────────────────────


def _write_spec(project: Path, name: str, labels: list[str]) -> None:
    spec_dir = project / ".aifactory" / "specs" / name
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "requirements.json").write_text(
        json.dumps(
            {
                "title": name,
                "description": "x",
                "githubIssue": {"number": 1, "labels": labels},
            }
        )
    )


async def test_list_tasks_orders_p0_ahead_of_p2(tmp_path, monkeypatch):
    from server.routes import tasks as tasks_mod

    project = tmp_path / "proj"
    # p2 spec created first; p0 spec created second — priority must beat recency.
    _write_spec(project, "001-low", ["pfactory", "handoff:aifactory", "priority:p2"])
    _write_spec(project, "002-crit", ["pfactory", "handoff:aifactory", "priority:p0"])

    monkeypatch.setattr(
        tasks_mod, "load_projects", lambda: {"p": {"path": str(project)}}
    )

    result = await tasks_mod.list_tasks(project_id="p", status=None)
    ordered = [t.spec_id for t in result.tasks]
    assert ordered.index("002-crit") < ordered.index("001-low")


async def test_list_tasks_unprioritised_sorts_after_prioritised(tmp_path, monkeypatch):
    from server.routes import tasks as tasks_mod

    project = tmp_path / "proj"
    _write_spec(project, "001-plain", ["bug"])  # no pfactory priority → rank 99
    _write_spec(project, "002-crit", ["pfactory", "handoff:aifactory", "priority:p1"])

    monkeypatch.setattr(
        tasks_mod, "load_projects", lambda: {"p": {"path": str(project)}}
    )

    result = await tasks_mod.list_tasks(project_id="p", status=None)
    ordered = [t.spec_id for t in result.tasks]
    assert ordered.index("002-crit") < ordered.index("001-plain")


# ── TFactory routing at task start ─────────────────────────────────────────


async def test_start_task_routes_tfactory_child_away_from_coder(tmp_path, monkeypatch):
    from server.routes import execution as exec_mod
    from server.routes.execution import StartTaskRequest

    project = tmp_path / "proj"
    _write_spec(project, "001-tests", ["pfactory", "handoff:tfactory", "type:testing"])

    monkeypatch.setattr(
        exec_mod, "load_projects", lambda: {"p": {"path": str(project)}}
    )

    result = await exec_mod.start_task("p:001-tests", StartTaskRequest(), None)
    assert result["routed_to"] == "tfactory"
    # Handoff marker written; the coder was never dispatched.
    assert (
        project / ".aifactory" / "specs" / "001-tests" / "TFACTORY_HANDOFF.md"
    ).exists()


async def test_start_task_does_not_reroute_aifactory_child(tmp_path, monkeypatch):
    from server.routes import execution as exec_mod
    from server.routes.execution import StartTaskRequest

    project = tmp_path / "proj"
    _write_spec(project, "001-feat", ["pfactory", "handoff:aifactory", "type:software"])

    monkeypatch.setattr(
        exec_mod, "load_projects", lambda: {"p": {"path": str(project)}}
    )

    # No implementation_plan.json and no requirements for spec creation beyond
    # what we wrote — the AIFactory path raises (needs spec creation), which
    # proves we did NOT short-circuit to TFactory. A tfactory route would have
    # returned a dict before reaching that point.
    routed_to_tfactory = False
    try:
        result = await exec_mod.start_task("p:001-feat", StartTaskRequest(), None)
        routed_to_tfactory = (
            isinstance(result, dict) and result.get("routed_to") == "tfactory"
        )
    except Exception:
        routed_to_tfactory = False
    assert routed_to_tfactory is False
    assert not (
        project / ".aifactory" / "specs" / "001-feat" / "TFACTORY_HANDOFF.md"
    ).exists()
