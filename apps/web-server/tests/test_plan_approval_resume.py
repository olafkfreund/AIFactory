"""Resume path must pass --force only on a real plan approval (#1922).

task_control.planApproved is the web-server's agent-immutable record of a human
approval; StartTask trusts it or a hash-valid review_state.json, never the raw
`approved` key.
"""

import asyncio
import json
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

_BACKEND = str(Path(__file__).resolve().parents[2] / "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from review.state import ReviewState  # noqa: E402
from server.routes import execution, plan_approval  # noqa: E402
from server.services import task_control  # noqa: E402
from server.services.agent_service import AgentService  # noqa: E402

PLAN = {
    "feature": "x",
    "phases": [
        {"phase": 1, "subtasks": [{"id": "1", "description": "d", "status": "pending"}]}
    ],
}


def _spec(tmp_path: Path, meta: dict[str, Any] | None = None) -> Path:
    spec_dir = tmp_path / "repo" / ".aifactory" / "specs" / "001-x"
    spec_dir.mkdir(parents=True)
    (spec_dir / "spec.md").write_text("# x\n")
    (spec_dir / "implementation_plan.json").write_text(json.dumps(PLAN))
    (spec_dir / "task_metadata.json").write_text(
        json.dumps({"requireReviewBeforeCoding": True} if meta is None else meta)
    )
    return spec_dir


# -- step 5: task_control.write_control ---------------------------------------
def test_plan_approved_set_and_clear(tmp_path: Path) -> None:
    task_control.record_plan_approval(tmp_path, "web_user")
    c = task_control.read_control(tmp_path)
    assert c["planApproved"] is True
    assert c["planApprovedBy"] == "web_user"
    assert c["planApprovedAt"]

    task_control.write_control(
        tmp_path, status="in_progress"
    )  # unrelated write keeps it
    assert task_control.read_control(tmp_path)["planApproved"] is True

    task_control.clear_plan_approval(tmp_path)
    c = task_control.read_control(tmp_path)
    assert not {"planApproved", "planApprovedBy", "planApprovedAt"} & set(c)


def test_plan_review_reason_clears_approval(tmp_path: Path) -> None:
    task_control.record_plan_approval(tmp_path, "web_user")
    task_control.write_control(
        tmp_path, status="human_review", review_reason="plan_review"
    )
    assert "planApproved" not in task_control.read_control(tmp_path)


# -- step 6: approve / reject routes -------------------------------------------
def _route(fn: Callable[..., Any], tmp_path: Path, *args: Any) -> Any:
    projects = {"p": {"path": str(tmp_path / "repo")}}
    with (
        patch.object(plan_approval, "load_projects", return_value=projects),
        patch("server.websockets.events.emit_task_status", new=AsyncMock()),
        patch.object(plan_approval, "audit_task_action", new=AsyncMock()),
    ):
        return asyncio.run(fn("p:001-x", *args, {}))


def test_approve_plan_records_plan_approved_even_without_plan_file(
    tmp_path: Path,
) -> None:
    spec_dir = _spec(tmp_path)
    (spec_dir / "implementation_plan.json").unlink()
    req = plan_approval.ApprovePlanRequest(auto_restart=False)
    _route(plan_approval.approve_plan, tmp_path, req)
    c = task_control.read_control(spec_dir)
    assert c["planApproved"] is True and c["planApprovedBy"] == "web_user"


def test_reject_plan_clears_plan_approved(tmp_path: Path) -> None:
    spec_dir = _spec(tmp_path)
    task_control.record_plan_approval(spec_dir, "web_user")
    _route(plan_approval.reject_plan, tmp_path, plan_approval.RejectPlanRequest())
    assert "planApproved" not in task_control.read_control(spec_dir)


# -- step 7: StartTask force decision -----------------------------------------
def _start(tmp_path: Path) -> tuple[Any, AsyncMock]:
    """Run start_task; return (response-or-None, start_task_execution mock)."""
    svc = MagicMock()
    svc.stop_stale_spec_creation = AsyncMock()
    svc.is_running_anywhere = AsyncMock(return_value=False)
    svc.start_task_execution = AsyncMock(return_value=SimpleNamespace(pid=1))
    projects = {"p": {"path": str(tmp_path / "repo")}}
    with (
        patch.object(execution, "load_projects", return_value=projects),
        patch.object(execution, "get_agent_service", return_value=svc),
        patch.object(execution, "emit_task_status", new=AsyncMock()),
    ):
        result = asyncio.run(
            execution.start_task(
                "p:001-x",
                execution.StartTaskRequest(),
                SimpleNamespace(state=SimpleNamespace()),
            )
        )
    return result, svc.start_task_execution


def _assert_held(spec_dir: Path, result: Any, start: AsyncMock) -> None:
    assert not start.called
    assert result["status"] == "human_review"
    assert task_control.read_control(spec_dir)["reviewReason"] == "plan_review"


def test_hashless_approval_does_not_resume(tmp_path: Path) -> None:
    spec_dir = _spec(tmp_path)
    (spec_dir / "review_state.json").write_text(json.dumps({"approved": True}))
    result, start = _start(tmp_path)
    _assert_held(spec_dir, result, start)


def test_valid_approval_resumes_with_force(tmp_path: Path) -> None:
    spec_dir = _spec(tmp_path)
    ReviewState().approve(spec_dir)
    _, start = _start(tmp_path)
    assert start.await_args is not None
    assert start.await_args.kwargs["force"] is True


def test_stale_hash_without_plan_approved_does_not_resume(tmp_path: Path) -> None:
    spec_dir = _spec(tmp_path)
    ReviewState().approve(spec_dir)
    (spec_dir / "implementation_plan.json").write_text(
        json.dumps({**PLAN, "feature": "changed"})
    )
    result, start = _start(tmp_path)
    _assert_held(spec_dir, result, start)


def test_plan_approved_resumes_even_with_stale_hash(tmp_path: Path) -> None:
    spec_dir = _spec(tmp_path)
    ReviewState().approve(spec_dir)
    task_control.record_plan_approval(spec_dir, "web_user")
    (spec_dir / "implementation_plan.json").write_text(
        json.dumps({**PLAN, "feature": "changed"})
    )
    _, start = _start(tmp_path)
    assert start.await_args is not None
    assert start.await_args.kwargs["force"] is True


def test_auto_simple_approval_is_valid(tmp_path: Path) -> None:
    spec_dir = _spec(tmp_path)
    ReviewState().approve(spec_dir, approved_by="auto-simple")
    assert ReviewState.load(spec_dir).is_approval_valid(spec_dir)


# -- step 8: a planner forgery in the worktree never changes the next Start ---
def test_worktree_forgery_does_not_reach_main_or_change_start(tmp_path: Path) -> None:
    spec_dir = _spec(tmp_path)  # main: review required, plan, no approval
    worktree_spec = (
        (tmp_path / "repo" / ".aifactory" / "worktrees" / "tasks" / "001-x")
        / ".aifactory"
        / "specs"
        / "001-x"
    )
    worktree_spec.mkdir(parents=True)
    (worktree_spec / "implementation_plan.json").write_text(json.dumps(PLAN))
    (worktree_spec / "task_metadata.json").write_text("{}")  # flag cleared
    ReviewState().approve(worktree_spec)  # correctly hashed forged approval
    (worktree_spec / "token_usage.json").write_text("{}")  # proves the sync ran

    asyncio.run(
        AgentService()._sync_worktree_files(
            tmp_path / "repo", "001-x", task_id="p:001-x"
        )
    )

    assert (spec_dir / "token_usage.json").exists()
    assert not (spec_dir / "review_state.json").exists()
    assert json.loads((spec_dir / "task_metadata.json").read_text()) == {
        "requireReviewBeforeCoding": True
    }
    result, start = _start(tmp_path)
    _assert_held(spec_dir, result, start)
