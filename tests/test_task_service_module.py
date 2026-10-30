"""Task helpers live in routes/task_service.py — #556 (god-file split).

Locks the contract: the helpers import standalone (no routes/tasks.py load), and
routes/tasks.py re-exports the SAME objects so existing
``from ..routes.tasks import spec_to_task`` callers are unchanged.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "web-server"))

_HELPER_NAMES = (
    "get_spec_dirs",
    "get_next_spec_id",
    "get_worktree_spec_dir",
    "sync_worktree_to_main_spec",
    "validate_done_status",
    "get_plan_with_worktree_sync",
    "load_spec_metadata",
    "spec_to_task",
    "map_backend_status_to_frontend",
    "get_execution_progress",
    "task_to_dict",
)


def test_task_service_is_self_contained():
    # Importing the helpers must NOT require importing routes.tasks.
    from server.routes import task_service

    for name in _HELPER_NAMES:
        assert hasattr(task_service, name), f"task_service missing {name}"


def test_routes_tasks_reexports_same_helpers():
    from server.routes import task_service, tasks

    for name in _HELPER_NAMES:
        assert getattr(tasks, name) is getattr(task_service, name), (
            f"routes.tasks.{name} is not the same object as task_service.{name}"
        )


def test_load_spec_metadata_tolerates_out_of_enum_subtask_status(tmp_path):
    # #942: a plan whose subtask carries an out-of-enum status ("ready", which an
    # LLM planner emits) must NOT crash load_spec_metadata / GET /api/tasks. The
    # status is coerced to a valid literal; one bad spec can't 500 the list.
    from server.routes.task_service import _coerce_subtask_status, load_spec_metadata

    assert _coerce_subtask_status("ready") == "pending"
    assert _coerce_subtask_status("running") == "in_progress"
    assert _coerce_subtask_status("done") == "completed"
    assert _coerce_subtask_status(None) == "pending"
    assert _coerce_subtask_status("nonsense") == "pending"
    assert _coerce_subtask_status("COMPLETED") == "completed"

    import json

    spec = tmp_path / "042-x"
    spec.mkdir()
    (spec / "requirements.json").write_text(
        json.dumps({"title": "t", "description": "d"})
    )
    (spec / "implementation_plan.json").write_text(
        json.dumps(
            {
                "phases": [
                    {
                        "subtasks": [
                            {"id": "s1", "description": "do a thing", "status": "ready"}
                        ]
                    }
                ]
            }
        )
    )
    meta = load_spec_metadata(spec)  # must not raise
    assert meta["subtasks"][0].status == "pending"


def test_load_spec_metadata_tolerates_float_id_and_malformed_subtask(tmp_path):
    # #941: an LLM planner sometimes persists a numeric subtask id (1.1 as a
    # float) or other off-type field. Subtask's strict types would 500 the whole
    # task list (and block dispatch, which loads specs through this helper). The
    # loader must coerce scalars and degrade gracefully, never raise.
    import json

    from server.routes.task_service import load_spec_metadata

    spec = tmp_path / "042-x"
    spec.mkdir()
    (spec / "requirements.json").write_text(
        json.dumps({"title": "t", "description": "d"})
    )
    (spec / "implementation_plan.json").write_text(
        json.dumps(
            {
                "phases": [
                    {
                        "subtasks": [
                            {"id": 1.1, "description": "float id", "status": "ready"},
                            {"id": "s2", "title": "ok", "status": "pending"},
                        ]
                    }
                ]
            }
        )
    )
    meta = load_spec_metadata(spec)  # must not raise
    assert [s.id for s in meta["subtasks"]] == ["1.1", "s2"]
    assert meta["subtasks"][0].status == "pending"  # 'ready' coerced


def test_task_rows_declare_the_correlation_key(tmp_path, monkeypatch):
    # #1775: CFactory's adapter keys a polled row on `correlationKey` first, so a
    # row without it silently splits from the task's events onto a bare key.
    import json

    from server.routes import task_service
    from server.routes.task_models import Task

    specs = tmp_path / ".aifactory" / "specs"
    for spec_id, reqs in (
        ("001-a", {"githubIssue": {"number": 7, "repo": "owner/repo"}}),
        ("002-b", {"title": "no issue"}),
    ):
        (specs / spec_id).mkdir(parents=True)
        (specs / spec_id / "requirements.json").write_text(json.dumps(reqs))
    monkeypatch.setattr(
        task_service, "load_projects", lambda: {"p": {"path": str(tmp_path)}}
    )

    def row(spec_id: str) -> dict:
        task = Task(
            id=f"p:{spec_id}",
            spec_id=spec_id,
            project_id="p",
            title=spec_id,
            description="",
            created_at="2026-10-15T00:00:00Z",
            updated_at="2026-10-15T00:00:00Z",
        )
        return task_service.task_to_dict(task)

    assert row("001-a")["correlationKey"] == "owner/repo#7"
    assert row("002-b")["correlationKey"] == "af-p:002-b"


def test_task_rows_surface_contract_hold_from_plan(tmp_path, monkeypatch):
    # #1709: task_to_dict copies a string contractHold from the spec's plan
    # into the row and leaves the key absent otherwise.
    import json

    from server.routes import task_service
    from server.routes.task_models import Task

    specs = tmp_path / ".aifactory" / "specs"
    for spec_id, plan in (
        ("001-held", {"contractHold": "contract held (held by server); normal build"}),
        ("002-clean", {"phases": []}),
    ):
        (specs / spec_id).mkdir(parents=True)
        (specs / spec_id / "implementation_plan.json").write_text(json.dumps(plan))
    monkeypatch.setattr(
        task_service, "load_projects", lambda: {"p": {"path": str(tmp_path)}}
    )

    def row(spec_id: str) -> dict:
        task = Task(
            id=f"p:{spec_id}",
            spec_id=spec_id,
            project_id="p",
            title=spec_id,
            description="",
            created_at="2026-10-15T00:00:00Z",
            updated_at="2026-10-15T00:00:00Z",
        )
        return task_service.task_to_dict(task)

    assert (
        row("001-held")["contractHold"]
        == "contract held (held by server); normal build"
    )
    assert "contractHold" not in row("002-clean")
