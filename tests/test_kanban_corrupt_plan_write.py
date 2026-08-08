"""A kanban status change must never replace an unreadable plan (#1081).

Both status-update paths in ``routes/tasks.py`` read the plan through
``get_plan_with_worktree_sync``, which returns ``{}`` when the file cannot be
parsed, and then wrote that dict straight back:

    plan["status"] = update.status
    plan_file.write_text(json.dumps(plan, indent=2))

So one drag-and-drop replaced phases, subtasks and verification with
``{"status": "done"}``. "I cannot read this" is never a licence to replace it --
the corrupt file was at least still diagnosable.

The same read also made ``validate_done_status`` vacuous: ``{}`` has no phases,
so the "all subtasks must be completed before done" gate returned ``(True, "")``
and approved a task nobody could evaluate.

Related: #1069 (the read path), Factory#431 (silent fallbacks).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "web-server"))
# Appended, never inserted: tests/ contains packages (secrets/, docker/, ...)
# whose names shadow the stdlib if they win the import race.
sys.path.append(str(Path(__file__).parent))

# Reuses the exact corruption shape a real build emitted (#1069): the escaped
# quotes land OUTSIDE any string, so no JSON parser will read the file.
from test_corrupt_plan_status import CORRUPT_PLAN  # noqa: E402

SPEC_ID = "096-add-an-is-palindrome-helper-py"
TASK_ID = f"p1:{SPEC_ID}"


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A registered project holding one spec whose plan does not parse."""
    from server.routes import task_service
    from server.routes import tasks as tasks_module

    spec_dir = tmp_path / ".aifactory" / "specs" / SPEC_ID
    spec_dir.mkdir(parents=True)
    (spec_dir / "implementation_plan.json").write_text(CORRUPT_PLAN)

    registry = {"p1": {"path": str(tmp_path)}}
    for module in (task_service, tasks_module):
        monkeypatch.setattr(module, "load_projects", lambda: registry)
    monkeypatch.setattr(task_service, "resolve_project_path", lambda _pid: tmp_path)
    return spec_dir


def _plan_text(spec_dir: Path) -> str:
    return (spec_dir / "implementation_plan.json").read_text()


async def test_status_change_does_not_overwrite_an_unreadable_plan(project):
    """The destructive path: PATCH /{task_id}/status used to truncate the plan."""
    from server.routes.tasks import TaskStatusUpdate, update_task_status

    before = _plan_text(project)

    with pytest.raises(HTTPException) as exc:
        await update_task_status(
            TASK_ID, TaskStatusUpdate(status="in_progress"), _access={}
        )

    assert _plan_text(project) == before, (
        "an unreadable plan was replaced by the kanban status write"
    )
    assert exc.value.status_code == 409
    # The operator has to learn the plan needs regenerating, so the parse
    # location travels in the response rather than only into a log nobody reads.
    assert "implementation_plan.json" in str(exc.value.detail)
    assert "line" in str(exc.value.detail).lower()


async def test_put_update_does_not_overwrite_an_unreadable_plan(project):
    """The second caller, ``update_task``, had the identical write."""
    from server.routes.task_models import TaskUpdate
    from server.routes.tasks import update_task

    before = _plan_text(project)

    with pytest.raises(HTTPException) as exc:
        await update_task(TASK_ID, TaskUpdate(status="in_progress"), _access={})

    assert _plan_text(project) == before
    assert exc.value.status_code == 409


async def test_done_is_not_vacuously_approved_on_an_unreadable_plan(project):
    """``validate_done_status({})`` returns (True, "") -- it must never be asked."""
    from server.routes.tasks import TaskStatusUpdate, update_task_status

    with pytest.raises(HTTPException) as exc:
        await update_task_status(TASK_ID, TaskStatusUpdate(status="done"), _access={})

    assert exc.value.status_code == 409
    assert _plan_text(project) == CORRUPT_PLAN


async def test_force_does_not_buy_a_write_over_an_unreadable_plan(project):
    """``force`` skips the completeness gate; it is not a licence to destroy."""
    from server.routes.tasks import TaskStatusUpdate, update_task_status

    with pytest.raises(HTTPException) as exc:
        await update_task_status(
            TASK_ID, TaskStatusUpdate(status="done", force=True), _access={}
        )

    assert exc.value.status_code == 409
    assert _plan_text(project) == CORRUPT_PLAN


async def test_control_store_is_not_written_when_the_plan_is_unreadable(project):
    """Refusing means refusing: no half-applied move behind the operator's back."""
    from server.routes.tasks import TaskStatusUpdate, update_task_status

    with pytest.raises(HTTPException):
        await update_task_status(TASK_ID, TaskStatusUpdate(status="done"), _access={})

    assert not (project / "task_control.json").exists()


async def test_a_readable_plan_still_updates_normally(tmp_path, monkeypatch):
    """Mutation check: the guard must not fire on a plan that parses.

    Without this, "never write" would pass every assertion above while breaking
    the kanban board outright.
    """
    from server.routes import task_service
    from server.routes import tasks as tasks_module
    from server.routes.tasks import TaskStatusUpdate, update_task_status

    spec_dir = tmp_path / ".aifactory" / "specs" / SPEC_ID
    spec_dir.mkdir(parents=True)
    good = {"phases": [{"phase": 1, "name": "Implement", "subtasks": []}]}
    (spec_dir / "implementation_plan.json").write_text(json.dumps(good))

    registry = {"p1": {"path": str(tmp_path)}}
    for module in (task_service, tasks_module):
        monkeypatch.setattr(module, "load_projects", lambda: registry)
    monkeypatch.setattr(task_service, "resolve_project_path", lambda _pid: tmp_path)

    task = await update_task_status(
        TASK_ID, TaskStatusUpdate(status="in_progress"), _access={}
    )

    written = json.loads(_plan_text(spec_dir))
    assert written["status"] == "in_progress"
    assert written["phases"] == good["phases"], "the plan body was not preserved"
    assert task.status == "in_progress"
    assert (spec_dir / "task_control.json").exists()


async def test_a_missing_plan_file_still_updates_normally(tmp_path, monkeypatch):
    """Mutation check: absent is not corrupt.

    ``read_plan`` is only called when the file exists, so a spec with no plan
    keeps its previous behaviour (seed the file with the board status).
    """
    from server.routes import task_service
    from server.routes import tasks as tasks_module
    from server.routes.tasks import TaskStatusUpdate, update_task_status

    spec_dir = tmp_path / ".aifactory" / "specs" / SPEC_ID
    spec_dir.mkdir(parents=True)

    registry = {"p1": {"path": str(tmp_path)}}
    for module in (task_service, tasks_module):
        monkeypatch.setattr(module, "load_projects", lambda: registry)
    monkeypatch.setattr(task_service, "resolve_project_path", lambda _pid: tmp_path)

    task = await update_task_status(
        TASK_ID, TaskStatusUpdate(status="backlog"), _access={}
    )

    assert json.loads(_plan_text(spec_dir)) == {"status": "backlog"}
    assert task.status == "backlog"


def test_get_plan_with_worktree_sync_surfaces_the_read_error(tmp_path):
    """The root cause: the helper swallowed the fault its callers needed."""
    from server.routes.task_service import get_plan_with_worktree_sync

    spec_dir = tmp_path / ".aifactory" / "specs" / SPEC_ID
    spec_dir.mkdir(parents=True)
    (spec_dir / "implementation_plan.json").write_text(CORRUPT_PLAN)

    plan, plan_file, error = get_plan_with_worktree_sync(tmp_path, SPEC_ID)

    assert plan == {}
    assert plan_file == spec_dir / "implementation_plan.json"
    assert error is not None and "line" in error

    # ...and reports None for a plan that parses.
    (spec_dir / "implementation_plan.json").write_text(json.dumps({"phases": []}))
    _plan, _file, error = get_plan_with_worktree_sync(tmp_path, SPEC_ID)
    assert error is None
