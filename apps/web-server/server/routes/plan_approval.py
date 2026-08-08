"""
Plan-approval routes (human-review checkpoint: approve / reject an implementation plan).

Extracted verbatim from ``routes/tasks.py`` (issue #556) as a behavior-preserving
sub-router. These endpoints are mounted onto the tasks router via
``include_router`` so the public paths and request/response shapes are unchanged:

    POST /{task_id}/approve-plan
    POST /{task_id}/reject-plan

The handlers depend only on already-extractable collaborators -- ``load_projects``
from ``routes/projects`` (imported lazily there to avoid a module-level cycle),
``require_task_access`` from ``routes/project_authz``, the ``task_control`` service,
and lazily-imported ``review`` / websocket / ``agent_service`` modules -- none of
which import this module, so lifting the cluster out cannot create a circular
import.
"""

import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from server.specpath import safe_spec_component

from ..services import task_control
from .project_authz import require_task_access
from .projects import load_projects

router = APIRouter()


class ApprovePlanRequest(BaseModel):
    """Request to approve a plan."""

    auto_restart: bool = Field(True, description="Auto-restart task after approval")


class RejectPlanRequest(BaseModel):
    """Request to reject a plan with feedback for the planner.

    Mirrors ApprovePlanRequest's shape but carries the operator's reason so the
    planner's next iteration sees it in the spec's review feedback log.
    """

    feedback: str | None = Field(
        None,
        description="Optional reason for rejection — gets recorded on the review state's feedback log.",
    )


@router.post("/{task_id}/approve-plan")
async def approve_plan(
    task_id: str,
    request: ApprovePlanRequest = ApprovePlanRequest(),
    _access: dict = Depends(require_task_access("member")),
):
    """Approve a task's plan to allow coding to proceed.

    When a task is in plan_review status (waiting for human approval),
    this endpoint marks the plan as approved and optionally restarts the task.
    """
    if ":" not in task_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid task ID format",
        )

    project_id, spec_id = task_id.split(":", 1)

    # Barrier BEFORE spec_id reaches any path expression (#1056). Path joins
    # collapse traversal silently, so validating after the join is too late.
    try:
        spec_id = safe_spec_component(spec_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid task ID format",
        ) from None
    projects = load_projects()

    if project_id not in projects:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found",
        )

    project_path = Path(projects[project_id]["path"])
    spec_dir = project_path / ".aifactory" / "specs" / spec_id

    if not spec_dir.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Task not found",
        )

    # Import ReviewState from backend
    import sys

    backend_path = Path(__file__).parent.parent.parent.parent / "backend"
    if str(backend_path) not in sys.path:
        sys.path.insert(0, str(backend_path))

    from review import ReviewState

    # Approve the plan
    review_state = ReviewState.load(spec_dir)
    review_state.approve(spec_dir, approved_by="web_user")

    # Update implementation_plan.json status back to in_progress
    plan_file = spec_dir / "implementation_plan.json"
    if plan_file.exists():
        try:
            import logging

            logger = logging.getLogger(__name__)
            logger.info(f"[ApprovePlan] Reading plan file: {plan_file}")
            plan = json.loads(plan_file.read_text())
            logger.info(
                f"[ApprovePlan] Current status: {plan.get('status')}, planStatus: {plan.get('planStatus')}, reviewReason: {plan.get('reviewReason')}"
            )

            # Update BOTH status and planStatus fields
            plan["status"] = "in_progress"
            plan["planStatus"] = "in_progress"
            plan.pop("reviewReason", None)

            plan_file.write_text(json.dumps(plan, indent=2))
            logger.info(
                "[ApprovePlan] Updated plan file - status: in_progress, planStatus: in_progress"
            )

            # Issue #259: approving the plan moves the task out of human_review;
            # record that in the agent-immutable control store and clear the
            # plan_review reason.
            task_control.write_control(
                spec_dir,
                status="in_progress",
                clear_review_reason=True,
                updated_by="web_user",
            )
        except (json.JSONDecodeError, OSError) as e:
            import logging

            logging.getLogger(__name__).error(
                f"[ApprovePlan] Failed to update plan file: {e}"
            )
    else:
        import logging

        logging.getLogger(__name__).warning(
            f"[ApprovePlan] Plan file does not exist: {plan_file}"
        )

    # Emit status change via WebSocket
    from ..websockets.events import emit_task_status

    await emit_task_status(task_id, "in_progress")

    auto_restarted = False

    # Auto-restart if requested
    if request.auto_restart:
        try:
            from ..services.agent_service import get_agent_service

            agent_service = get_agent_service()

            # Clean up stale spec creation process if still tracked as running.
            # The spec_runner process may have exited but the monitor may not have
            # cleaned up running_tasks (e.g., if the process hung or monitor failed).
            if agent_service.is_running(task_id):
                import logging

                logger = logging.getLogger(__name__)
                logger.info(
                    f"[ApprovePlan] Cleaning up stale spec creation process for {task_id}"
                )
                try:
                    await agent_service.stop_task(task_id)
                except Exception as stop_err:
                    logger.warning(
                        f"[ApprovePlan] Failed to stop stale process: {stop_err}"
                    )
                    # Force-remove from running_tasks as fallback
                    agent_service.running_tasks.pop(task_id, None)

            # Read mode from task_metadata.json
            task_metadata_file = spec_dir / "task_metadata.json"
            mode = "full"
            if task_metadata_file.exists():
                try:
                    metadata = json.loads(task_metadata_file.read_text())
                    mode = metadata.get("mode", "full")
                except (json.JSONDecodeError, OSError):
                    pass

            await agent_service.start_task_execution(
                task_id=task_id,
                project_path=project_path,
                spec_id=spec_id,
                auto_continue=True,
                mode=mode,
                force=True,  # Bypass approval check since plan was manually approved
            )
            auto_restarted = True
        except Exception as e:
            # If auto-restart fails, still return success for approval
            import logging

            logging.getLogger(__name__).warning(
                f"Auto-restart failed for {task_id}: {e}"
            )

    return {
        "success": True,
        "task_id": task_id,
        "message": "Plan approved" + (" and task restarted" if auto_restarted else ""),
        "autoRestarted": auto_restarted,
    }


@router.post("/{task_id}/reject-plan")
async def reject_plan(
    task_id: str,
    request: RejectPlanRequest = RejectPlanRequest(),
    _access: dict = Depends(require_task_access("member")),
):
    """Reject a task's plan and send the planner back to iterate.

    Used by the human-review checkpoint when the implementation plan needs
    rework. The optional ``feedback`` field is appended to the spec's
    review-state feedback log so the planner's next pass picks it up.
    """
    if ":" not in task_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid task ID format"
        )

    project_id, spec_id = task_id.split(":", 1)

    # Barrier BEFORE spec_id reaches any path expression (#1056). Path joins
    # collapse traversal silently, so validating after the join is too late.
    try:
        spec_id = safe_spec_component(spec_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid task ID format",
        ) from None
    projects = load_projects()
    if project_id not in projects:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Project not found"
        )

    project_path = Path(projects[project_id]["path"])
    spec_dir = project_path / ".aifactory" / "specs" / spec_id
    if not spec_dir.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Task not found"
        )

    # Import ReviewState the same way approve_plan does (sys.path shim
    # because the web-server doesn't have ``backend`` on its PYTHONPATH
    # in every install layout).
    import sys

    backend_path = Path(__file__).parent.parent.parent.parent / "backend"
    if str(backend_path) not in sys.path:
        sys.path.insert(0, str(backend_path))

    from review.state import ReviewState

    review_state = ReviewState.load(spec_dir)
    review_state.reject(spec_dir)
    if request.feedback:
        review_state.add_feedback(request.feedback, spec_dir=spec_dir)

    # Mirror approve_plan's bookkeeping: flip the plan back to "needs work"
    # so the next planner pass sees a clean slate.
    plan_file = spec_dir / "implementation_plan.json"
    if plan_file.exists():
        try:
            plan = json.loads(plan_file.read_text())
            plan["status"] = "rejected"
            plan["planStatus"] = "rejected"
            if request.feedback:
                plan["reviewReason"] = request.feedback
            plan_file.write_text(json.dumps(plan, indent=2))
        except (OSError, json.JSONDecodeError) as exc:
            # Plan file unreadable — review state was already updated, so
            # the reject took effect even if the bookkeeping fails. Log
            # and continue.
            import logging

            logging.getLogger(__name__).warning(
                f"[RejectPlan] couldn't update implementation_plan.json: {exc}"
            )

    return {
        "success": True,
        "task_id": task_id,
        "feedback_recorded": bool(request.feedback),
    }
