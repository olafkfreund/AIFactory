"""
Task management routes.

Handles CRUD operations for tasks (specs) within projects.
"""

import ast
import json
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..database.engine import get_db
from ..paths import get_data_dir
from ..services import task_control

# PR-creation route (POST /{task_id}/worktree/create-pr) and its request model
# were extracted into ``routes/pr.py`` (issue #556) and are mounted via
# ``include_router`` below. The names are re-exported here so existing imports
# (``from ..routes.tasks import CreatePRFromTaskOptions, create_pr_from_task``,
# used by ``mcp_stdio/router.py``) keep working.
from .inbox import (  # noqa: F401
    InboxEnqueueResponse,
    InboxMessage,
    InboxMessageCreate,
    enqueue_inbox_message,
    list_inbox_messages,
)
from .inbox import router as inbox_router
from .pr import CreatePRFromTaskOptions, create_pr_from_task  # noqa: F401
from .pr import router as pr_router
from .project_authz import accessible_org_ids, require_task_access
from .projects import get_projects_file, load_projects
from .worktree_tools import (
    OpenInIDERequest,
    OpenInTerminalRequest,
    detect_worktree_tools,
    get_ide_command,
    get_terminal_command,
    open_worktree_in_ide,
    open_worktree_in_terminal,
)
from .worktree_tools import router as worktree_tools_router

router = APIRouter()


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------
# The core task models live in routes/task_models.py (#556) so sub-routers can
# import them without importing this module (which would be circular). They are
# re-exported here unchanged so existing ``from ..routes.tasks import Task``
# callers keep working.
from .task_models import (  # noqa: F401
    ClarificationAnswer,
    ClarificationAnswersRequest,
    ClarificationQuestion,
    ClarificationResponse,
    SelectedSkill,
    Subtask,
    SubtaskVerification,
    Task,
    TaskBase,
    TaskCreate,
    TaskList,
    TaskMetadata,
    TaskMetadataUpdate,
    TaskStatus,
    TaskUpdate,
)


# --------------------------------------------------------------------------
# Helper Functions
# --------------------------------------------------------------------------
# The spec/worktree/plan/serialization helpers live in routes/task_service.py
# (#556) so this file is a thin routing layer. They are re-exported here so
# existing ``from ..routes.tasks import spec_to_task`` callers are unchanged.
from .task_service import (  # noqa: F401
    _clean_task_description,
    _collapse_correlation_epic,
    _looks_like_stringified_mapping,
    _pfactory_priority_rank,
    _resolve_task,
    _summarize_mapping,
    get_execution_progress,
    get_next_spec_id,
    get_plan_with_worktree_sync,
    get_spec_dirs,
    get_worktree_spec_dir,
    load_spec_metadata,
    map_backend_status_to_frontend,
    overlay_durable_status,
    project_repo,
    spec_to_task,
    sync_worktree_to_main_spec,
    task_to_dict,
    validate_done_status,
)


@router.get("", response_model=TaskList)
async def list_tasks(
    project_id: str | None = Query(None, description="Filter by project ID"),
    status: TaskStatus | None = Query(None, description="Filter by status"),
    request: Request = None,
    db: AsyncSession = Depends(get_db),
):
    """List tasks visible to the caller, optionally filtered by project/status.

    Tenant isolation (#319): a human user only sees tasks in projects owned by
    an org they belong to; the service principal (local UI / siblings) sees all.
    """
    projects = load_projects()

    # Filter projects
    if project_id:
        if project_id not in projects:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Project not found",
            )
        project_ids = [project_id]
    else:
        project_ids = list(projects.keys())

    # Restrict to projects in the caller's orgs (skipped for direct callers /
    # service principal, where accessible_org_ids returns None).
    if request is not None and isinstance(db, AsyncSession):
        allowed = await accessible_org_ids(request, db)
        if allowed is not None:
            project_ids = [
                pid
                for pid in project_ids
                if projects.get(pid, {}).get("org_id") in allowed
            ]

    # Collect tasks from all projects
    all_tasks = []
    priority_ranks: dict[str, int] = {}
    for pid in project_ids:
        project_path = Path(projects[pid]["path"])
        repo = project_repo(projects[pid])  # W5 (#218): target repo on every task
        spec_dirs = get_spec_dirs(project_path)
        for spec_dir in spec_dirs:
            task = spec_to_task(pid, spec_dir)
            task.repo = repo
            all_tasks.append(task)
            priority_ranks[task.id] = _pfactory_priority_rank(spec_dir)

    # W2 (Factory #218): correct any task left at the stale ``backlog`` default
    # with the authoritative durable lifecycle, BEFORE the status filter so a
    # corrected status (e.g. in_progress) is filtered on its real value.
    await overlay_durable_status(all_tasks)

    if status is not None:
        all_tasks = [task for task in all_tasks if task.status == status]

    # Sort by created_at descending, then stably by PFactory priority (epic #327
    # / #331): governed children with priority:p0 are scheduled ahead of p2,
    # while tasks with no PFactory priority keep their newest-first ordering.
    all_tasks.sort(key=lambda t: t.created_at, reverse=True)
    all_tasks.sort(key=lambda t: priority_ranks.get(t.id, 99))

    return TaskList(tasks=all_tasks, total=len(all_tasks))


@router.get("/{task_id}")
async def get_task(
    task_id: str, _access: dict = Depends(require_task_access("viewer"))
):
    """Get a specific task by ID.

    Returns full task details including execution progress and metadata
    (archivedAt, archivedInVersion).
    """
    # Parse task ID (format: project_id:spec_id)
    if ":" not in task_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid task ID format. Expected 'project_id:spec_id'",
        )

    project_id, spec_id = task_id.split(":", 1)
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

    task = spec_to_task(project_id, spec_dir)
    return task_to_dict(task)


@router.post("", response_model=Task, status_code=status.HTTP_201_CREATED)
async def create_task(task: TaskCreate):
    """Create a new task (spec) in a project."""
    projects = load_projects()

    if task.project_id not in projects:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found",
        )

    project_path = Path(projects[task.project_id]["path"])

    # Ensure .aifactory/specs exists
    specs_dir = project_path / ".aifactory" / "specs"
    specs_dir.mkdir(parents=True, exist_ok=True)

    # Generate spec ID and create directory
    spec_id = get_next_spec_id(project_path, task.title)
    spec_dir = specs_dir / spec_id
    spec_dir.mkdir()

    # Create initial spec.md
    spec_content = f"""# {task.title}

{task.description}

## Acceptance Criteria

- [ ] Feature works as described
- [ ] Tests pass
- [ ] Code review approved

## Notes

Created via Magestic AI Web UI
"""
    (spec_dir / "spec.md").write_text(spec_content)

    # Create requirements.json with metadata
    requirements: dict = {
        "title": task.title,
        "description": task.description,
        "created_at": datetime.now().isoformat(),
    }

    # Add metadata if provided
    if task.metadata:
        metadata_dict = task.metadata.model_dump(exclude_none=True)
        if metadata_dict:
            requirements["metadata"] = metadata_dict

            # Sync task_metadata.json for phase_config.py to read model/thinking settings
            # Also include selectedSkills so agent_service.py can inject skill context
            model_fields = [
                "model",
                "thinkingLevel",
                "isAutoProfile",
                "phaseModels",
                "phaseThinking",
                "mode",
                "selectedSkills",
            ]
            task_metadata = {
                field: metadata_dict[field]
                for field in model_fields
                if field in metadata_dict
            }
            if task_metadata:
                (spec_dir / "task_metadata.json").write_text(
                    json.dumps(task_metadata, indent=2)
                )

    (spec_dir / "requirements.json").write_text(json.dumps(requirements, indent=2))

    return spec_to_task(task.project_id, spec_dir)


# --------------------------------------------------------------------------
# Clarification Endpoints
# --------------------------------------------------------------------------
#
# _resolve_task now lives in routes/task_service.py (#769) and is re-exported
# above, so ``from .tasks import _resolve_task`` continues to work unchanged.


def _try_close_github_issue(project_path: Path, spec_dir: Path) -> None:
    """Try to close a linked GitHub issue. Logs but doesn't raise on failure."""
    try:
        req_file = spec_dir / "requirements.json"
        if not req_file.exists():
            return
        reqs = json.loads(req_file.read_text())
        # Check metadata.githubIssueNumber (set by task creation from issue)
        issue_number = None
        if isinstance(reqs.get("metadata"), dict):
            issue_number = reqs["metadata"].get("githubIssueNumber")
        # Also check githubIssue.number (set by import endpoint)
        if not issue_number and isinstance(reqs.get("githubIssue"), dict):
            issue_number = reqs["githubIssue"].get("number")
        if not issue_number:
            return
        from .github import run_gh_command

        result = run_gh_command(
            ["issue", "close", str(issue_number)],
            cwd=str(project_path),
        )
        if result["success"]:
            import logging

            logging.getLogger(__name__).info(
                f"Auto-closed GitHub issue #{issue_number}"
            )
        else:
            import logging

            logging.getLogger(__name__).warning(
                f"Failed to auto-close GitHub issue #{issue_number}: {result.get('error', 'unknown')}"
            )
    except Exception as e:
        import logging

        logging.getLogger(__name__).warning(f"Error auto-closing GitHub issue: {e}")


class TaskStatusUpdate(BaseModel):
    """Model for updating only task status (for kanban)."""

    status: TaskStatus
    force: bool = False  # Skip validation (e.g., after successful merge)


@router.patch("/{task_id}/status", response_model=Task)
async def update_task_status(
    task_id: str,
    update: TaskStatusUpdate,
    _access: dict = Depends(require_task_access("member")),
):
    """Update a task's status (for kanban drag-and-drop)."""
    if ":" not in task_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid task ID format",
        )

    project_id, spec_id = task_id.split(":", 1)
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

    # Sync from worktree first to get latest progress
    plan, plan_file = get_plan_with_worktree_sync(project_path, spec_id)

    # Validate "done" status - ensure all subtasks are completed
    # Skip validation when force=True (e.g., after successful merge)
    if update.status == "done" and not update.force:
        is_valid, error_msg = validate_done_status(plan)
        if not is_valid:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=error_msg,
            )

    plan["status"] = update.status
    plan_file.write_text(json.dumps(plan, indent=2))

    # Issue #259: control-plane state (board column / status) is authoritative
    # in the dedicated, agent-immutable store. Moving out of a human-review
    # column clears the reviewReason.
    task_control.write_control(
        spec_dir,
        status=update.status,
        clear_review_reason=update.status
        in ("backlog", "in_progress", "ai_review", "done"),
        updated_by="web_user",
    )

    # Auto-close linked GitHub issue when task is marked done
    if update.status == "done":
        _try_close_github_issue(project_path, spec_dir)
        # Emit the RFC-0001 completion event so CFactory can thread the unit of
        # work end to end (best-effort — never breaks the request). #342.
        try:
            from ..services.completion import emit_terminal_completion

            emit_terminal_completion(
                spec_dir,
                task_id=task_id,
                project_id=project_id,
                spec_id=spec_id,
                status=update.status,
            )
        except Exception:  # pragma: no cover - notification is best-effort
            import logging

            logging.getLogger(__name__).debug("completion emit failed", exc_info=True)

    return spec_to_task(project_id, spec_dir)


@router.put("/{task_id}", response_model=Task)
@router.patch("/{task_id}", response_model=Task)
async def update_task(
    task_id: str,
    update: TaskUpdate,
    _access: dict = Depends(require_task_access("member")),
):
    """Update a task's metadata (supports both PUT and PATCH)."""
    if ":" not in task_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid task ID format",
        )

    project_id, spec_id = task_id.split(":", 1)
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

    # Update spec.md if title/description changed
    if update.title or update.description:
        spec_file = spec_dir / "spec.md"
        current_content = spec_file.read_text() if spec_file.exists() else ""

        if update.title:
            # Replace first heading
            current_content = re.sub(
                r"^#\s+.+$",
                f"# {update.title}",
                current_content,
                count=1,
                flags=re.MULTILINE,
            )

        if update.description:
            # Replace description paragraph (second section after title)
            # Split by double newline: [title, description, rest...]
            sections = current_content.split("\n\n", 2)
            if len(sections) >= 2:
                sections[1] = update.description
                current_content = "\n\n".join(sections)

        spec_file.write_text(current_content)

    # Update status in implementation_plan.json
    if update.status:
        # Sync from worktree first to get latest progress
        plan, plan_file = get_plan_with_worktree_sync(project_path, spec_id)

        # Validate "done" status - ensure all subtasks are completed
        if update.status == "done":
            is_valid, error_msg = validate_done_status(plan)
            if not is_valid:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=error_msg,
                )

        plan["status"] = update.status
        plan_file.write_text(json.dumps(plan, indent=2))

        # Issue #259: mirror to the agent-immutable control store.
        task_control.write_control(
            spec_dir,
            status=update.status,
            clear_review_reason=update.status
            in ("backlog", "in_progress", "ai_review", "done"),
            updated_by="web_user",
        )

    # Update requirements.json with title, description, and metadata
    requirements_file = spec_dir / "requirements.json"
    if update.title or update.description or update.metadata:
        requirements = {}
        if requirements_file.exists():
            try:
                requirements = json.loads(requirements_file.read_text())
            except json.JSONDecodeError:
                pass

        if update.title:
            requirements["title"] = update.title
        if update.description:
            requirements["description"] = update.description

        if update.metadata:
            if "metadata" not in requirements:
                requirements["metadata"] = {}

            # Get all fields that were explicitly set in the request (including None/null)
            # model_dump(exclude_unset=True) returns only fields that were explicitly set
            metadata_dict = update.metadata.model_dump(exclude_unset=True)

            # Process each field: null values clear the field, non-null values update it
            for field, value in metadata_dict.items():
                if value is None:
                    # Explicitly clear this field
                    requirements["metadata"].pop(field, None)
                else:
                    # Update the field
                    requirements["metadata"][field] = value

            # Sync task_metadata.json for phase_config.py to read model/thinking settings
            task_metadata_file = spec_dir / "task_metadata.json"
            task_metadata = {}
            if task_metadata_file.exists():
                try:
                    task_metadata = json.loads(task_metadata_file.read_text())
                except json.JSONDecodeError:
                    pass

            # Update model-related fields that phase_config.py expects
            # Also include selectedSkills so agent_service.py can inject skill context
            model_fields = [
                "model",
                "thinkingLevel",
                "isAutoProfile",
                "phaseModels",
                "phaseThinking",
                "mode",
                "requireReviewBeforeCoding",
                "selectedSkills",
            ]
            for field in model_fields:
                if field in metadata_dict:
                    if metadata_dict[field] is None:
                        # Clear field from task_metadata
                        task_metadata.pop(field, None)
                    else:
                        task_metadata[field] = metadata_dict[field]

            if task_metadata:
                task_metadata_file.write_text(json.dumps(task_metadata, indent=2))
            elif task_metadata_file.exists():
                # If all model fields were cleared, remove the file
                task_metadata_file.unlink()

        requirements_file.write_text(json.dumps(requirements, indent=2))

    return spec_to_task(project_id, spec_dir)


@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_task(
    task_id: str, _access: dict = Depends(require_task_access("admin"))
):
    """Delete a task (removes spec directory)."""
    if ":" not in task_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid task ID format",
        )

    project_id, spec_id = task_id.split(":", 1)
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

    # Remove directory (recursively)
    import shutil

    shutil.rmtree(spec_dir)


# Plan-approval routes (POST /{task_id}/approve-plan, /{task_id}/reject-plan)
# and their request models were extracted into ``routes/plan_approval.py``
# (issue #556) and are mounted via ``include_router`` below. The names are
# re-exported here so existing imports (``from .tasks import approve_plan`` /
# ``ApprovePlanRequest`` / ``reject_plan`` / ``RejectPlanRequest``) keep working.
from .plan_approval import (  # noqa: E402,F401
    ApprovePlanRequest,
    RejectPlanRequest,
    approve_plan,
    reject_plan,
)
from .plan_approval import router as plan_approval_router


# ============================================
# Worktree Merge Routes
# ============================================


# ============================================
# Worktree merge / conflict-resolution Routes (#649, epic #154)
# ============================================
#
# Extracted into ``routes/worktree_merge.py`` as a behavior-preserving
# sub-router. It is mounted here so the public paths are unchanged:
#   GET  /{task_id}/worktree/merge-preview
#   POST /{task_id}/worktree/resolve-conflicts
#   POST /{task_id}/worktree/resolve-uncommitted
#   POST /{task_id}/worktree/resolve-git-merge
#   POST /{task_id}/worktree/abort-merge
#   POST /{task_id}/worktree/merge
#   GET  /{task_id}/worktree/status
#   GET  /{task_id}/worktree/diff
#   POST /{task_id}/worktree/discard
#
# The models and handlers are re-exported below so existing imports keep working
# (``mcp_stdio/router.py`` imports ``WorktreeMergeOptions``, ``merge_worktree``,
# and ``get_worktree_diff`` from ``..routes.tasks``).
from .worktree_merge import (  # noqa: E402,F401
    ConflictResolveOptions,
    WorktreeMergeOptions,
    abort_worktree_merge,
    discard_worktree,
    get_worktree_diff,
    get_worktree_merge_preview,
    get_worktree_status,
    merge_worktree,
    resolve_git_merge_conflicts,
    resolve_uncommitted_conflicts,
    resolve_worktree_conflicts,
)
from .worktree_merge import router as worktree_merge_router

router.include_router(worktree_merge_router)

# Task log endpoints — extracted into routes/tasks_logs.py (#556); mounted here
# so the public /api/tasks/{task_id}/logs paths are unchanged. get_task_logs is
# re-exported so existing callers (mcp_stdio/router.py, projects.py) that import
# it from ..routes.tasks keep working.
from .tasks_logs import get_task_logs  # noqa: E402,F401
from .tasks_logs import router as tasks_logs_router  # noqa: E402

router.include_router(tasks_logs_router)

# Task read-view endpoints — extracted into routes/tasks_views.py (#556);
# mounted here so the public /api/tasks paths are unchanged.
from .tasks_views import router as tasks_views_router  # noqa: E402

router.include_router(tasks_views_router)

# Task usage endpoints — extracted into routes/tasks_usage.py (#556); mounted
# here so the public /api/tasks paths are unchanged.
from .tasks_usage import router as tasks_usage_router  # noqa: E402

router.include_router(tasks_usage_router)

# Task clarification endpoints — extracted into routes/tasks_clarifications.py
# (#556); mounted here so the public /api/tasks paths are unchanged.
from .tasks_clarifications import router as tasks_clarifications_router  # noqa: E402

router.include_router(tasks_clarifications_router)


# ============================================
# Worktree Open in IDE/Terminal Routes (#556)
# ============================================
#
# Extracted into ``routes/worktree_tools.py`` as a behavior-preserving
# sub-router. It is mounted here so the public paths are unchanged:
#   POST /worktree/open-in-ide, /worktree/open-in-terminal, /worktree/detect-tools
router.include_router(worktree_tools_router)

# ============================================
# Plan-approval Routes (#556)
# ============================================
#
# Extracted into ``routes/plan_approval.py`` as a behavior-preserving
# sub-router. It is mounted here so the public paths are unchanged:
#   POST /{task_id}/approve-plan, /{task_id}/reject-plan
router.include_router(plan_approval_router)

# ============================================
# PR-creation Route (#556)
# ============================================
#
# Extracted into ``routes/pr.py`` as a behavior-preserving sub-router. It is
# mounted here so the public path is unchanged:
#   POST /{task_id}/worktree/create-pr
router.include_router(pr_router)

# Backward-compatible re-exports: these names historically lived in this
# module. Nothing in-tree imports them today, but keep the public surface of
# ``routes.tasks`` stable so the extraction is a pure no-op for any importer.
__all_worktree_tools__ = (
    OpenInIDERequest,
    OpenInTerminalRequest,
    get_ide_command,
    get_terminal_command,
    open_worktree_in_ide,
    open_worktree_in_terminal,
    detect_worktree_tools,
)


# ============================================
# Inbox / inter-agent messaging Routes (#556, #264)
# ============================================
#
# Extracted into ``routes/inbox.py`` as a behavior-preserving sub-router. It is
# mounted here so the public paths are unchanged:
#   POST /{task_id}/inbox, GET /{task_id}/inbox
router.include_router(inbox_router)

# Backward-compatible re-exports: the inbox models and handlers historically
# lived in this module. Nothing in-tree imports them today, but keep the public
# surface of ``routes.tasks`` stable so the extraction is a pure no-op.
__all_inbox__ = (
    InboxMessageCreate,
    InboxMessage,
    InboxEnqueueResponse,
    enqueue_inbox_message,
    list_inbox_messages,
)
