"""A corrupt implementation_plan.json must not become a plausible wrong status (#1069).

Two separate faults, one symptom. A task straight off a successful build reported
``human_review`` from ``GET /api/projects/{pid}/tasks`` and ``backlog`` from
``GET /api/tasks/{task_id}`` at the same moment:

1. Its plan file was unparseable JSON, and both load sites swallowed the
   ``JSONDecodeError`` with a bare ``pass``. No status was found, so the
   ``backlog`` default won -- and ``backlog`` asserts the task has NOT started,
   a claim the code cannot support once it has failed to read the file.
2. Only the list endpoint applied the durable-lifecycle overlay, so the two
   endpoints derived status differently. That divergence is the only reason the
   corruption was visible at all.

Part of the fleet-wide "silent fallbacks" pattern (Factory#431).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "web-server"))

# The exact shape the build emitted: a test-case string containing an empty-string
# literal, escaped as though it were inside a shell string, so the quotes land
# OUTSIDE any string and no JSON parser will read the file.
CORRUPT_PLAN = """{
  "phases": [
    {
      "phase": 1,
      "name": "Implement",
      "subtasks": [
        {
          "id": "1.1",
          "title": "is_palindrome",
          "status": "completed",
          "cases": [
            "'hello' -> False",
            \\"'' -> False\\",
            "'Was it a car or a cat I saw?' -> True"
          ]
        }
      ]
    }
  ]
}
"""


def _spec_with_corrupt_plan(tmp_path: Path) -> Path:
    spec_dir = tmp_path / ".aifactory" / "specs" / "096-add-an-is-palindrome-helper-py"
    spec_dir.mkdir(parents=True)
    (spec_dir / "implementation_plan.json").write_text(CORRUPT_PLAN)
    return spec_dir


def test_the_fixture_really_is_unparseable(tmp_path):
    # Guards the test itself: if this ever parses, every assertion below is vacuous.
    spec_dir = _spec_with_corrupt_plan(tmp_path)
    try:
        json.loads((spec_dir / "implementation_plan.json").read_text())
    except json.JSONDecodeError:
        return
    raise AssertionError("fixture parsed; it must be invalid JSON to exercise #1069")


def test_corrupt_plan_is_not_reported_as_backlog(tmp_path, caplog):
    """An unreadable plan is a fault, not evidence the task is queued."""
    from server.routes.task_service import load_spec_metadata

    spec_dir = _spec_with_corrupt_plan(tmp_path)

    with caplog.at_level("ERROR"):
        metadata = load_spec_metadata(spec_dir)

    assert metadata["status"] != "backlog", (
        "a task whose plan could not be read was reported as queued/not-started"
    )
    assert metadata["status"] == "plan_unreadable"
    assert metadata["reviewReason"] == "plan_unreadable"

    # The fault must name itself: path and parse offset, not a bare pass.
    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "implementation_plan.json" in logged
    assert "line" in logged.lower() or "char" in logged.lower()


def test_plan_unreadable_reaches_a_column_the_board_renders():
    """A new backend token must be mapped, or it lands right back at backlog.

    ``map_backend_status_to_frontend`` defaults anything unknown to ``backlog``,
    and the Kanban board silently DROPS a card whose status is not one of its
    five columns -- so an unmapped token would either restore the bug or make
    the task invisible.
    """
    from server.routes.task_service import map_backend_status_to_frontend

    frontend = map_backend_status_to_frontend("plan_unreadable")
    assert frontend != "backlog"
    assert frontend in {"backlog", "in_progress", "ai_review", "human_review", "done"}


def test_corrupt_plan_does_not_crash_the_task_serializer(tmp_path, monkeypatch):
    """One corrupt spec must not 500 the task list."""
    from server.routes import task_service

    spec_dir = _spec_with_corrupt_plan(tmp_path)
    monkeypatch.setattr(
        task_service, "load_projects", lambda: {"p1": {"path": str(tmp_path)}}
    )
    monkeypatch.setattr(task_service, "resolve_project_path", lambda _pid: tmp_path)

    task = task_service.spec_to_task("p1", spec_dir)
    assert task.status == "human_review"
    assert task.review_reason == "plan_unreadable"


async def test_worktree_sync_refuses_to_publish_an_unparseable_plan(tmp_path, caplog):
    """Fail at write time, where the cause is one line away.

    The sync used to fall back to a verbatim ``shutil.copy2`` when it could not
    parse the worktree's plan, which is how an unparseable file reached the spec
    dir the control plane reads.
    """
    from server.services.agent_service import AgentService

    spec_id = "096-add-an-is-palindrome-helper-py"
    project_path = tmp_path / "proj"
    main_spec = project_path / ".aifactory" / "specs" / spec_id
    worktree_spec = (
        project_path
        / ".aifactory"
        / "worktrees"
        / "tasks"
        / spec_id
        / ".aifactory"
        / "specs"
        / spec_id
    )
    main_spec.mkdir(parents=True)
    worktree_spec.mkdir(parents=True)

    good = {"phases": [{"phase": 1, "name": "Implement", "subtasks": []}]}
    (main_spec / "implementation_plan.json").write_text(json.dumps(good))
    (worktree_spec / "implementation_plan.json").write_text(CORRUPT_PLAN)

    with caplog.at_level("ERROR"):
        await AgentService()._sync_worktree_files(
            project_path, spec_id, task_id=f"proj:{spec_id}"
        )

    assert json.loads((main_spec / "implementation_plan.json").read_text()) == good, (
        "an unparseable worktree plan was published over the last good copy"
    )
    assert "implementation_plan.json" in " ".join(
        record.getMessage() for record in caplog.records
    )


async def test_list_and_detail_derive_status_the_same_way(tmp_path, monkeypatch):
    """The list and the detail page must not disagree about the same task.

    Only the list applied the durable-lifecycle overlay, so a task whose
    task_logs.json never reached the control plane read ``human_review`` on the
    board and ``backlog`` on the page the board links to.
    """
    from server.routes import projects as projects_module
    from server.routes import task_service
    from server.routes import tasks as tasks_module

    # No plan file at all: an honest ``backlog`` default, so this test isolates
    # the list/detail divergence from the corrupt-plan fix above.
    spec_dir = tmp_path / ".aifactory" / "specs" / "001-queued"
    spec_dir.mkdir(parents=True)

    registry = {"p1": {"path": str(tmp_path)}}
    for module in (projects_module, task_service, tasks_module):
        if hasattr(module, "load_projects"):
            monkeypatch.setattr(module, "load_projects", lambda: registry)
    monkeypatch.setattr(task_service, "resolve_project_path", lambda _pid: tmp_path)

    async def fake_overlay(tasks):
        for task in tasks:
            if task.status == "backlog":
                task.status = "in_progress"

    monkeypatch.setattr(tasks_module, "overlay_durable_status", fake_overlay)

    listed = await projects_module.list_project_tasks("p1", None, _access={})
    detail = await tasks_module.get_task("p1:001-queued", _access={})

    assert listed[0]["status"] == detail["status"], (
        f"list said {listed[0]['status']!r}, detail said {detail['status']!r}"
    )
