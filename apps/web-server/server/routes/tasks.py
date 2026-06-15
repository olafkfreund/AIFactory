"""
Task management routes.

Handles CRUD operations for tasks (specs) within projects.
"""

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


# Frontend-compatible task statuses (matches frontend KanbanBoard columns)
TaskStatus = Literal[
    "backlog",
    "in_progress",
    "ai_review",
    "human_review",
    "done",
    # Copilot cloud-agent delegation statuses (set by copilot_dispatch_service)
    "copilot_running",
    "copilot_pr_opened",
]

# Backend statuses that get mapped to frontend statuses:
# backlog -> backlog           (not started)
# planning -> backlog          (still in queue)
# in_progress -> in_progress   (actively building)
# review -> human_review       (build finished, needs merge review)
# qa_pending -> ai_review      (QA running)
# qa_failed -> human_review    (QA failed, needs human attention)
# completed -> human_review    (finished, needs final approval/merge)
# cancelled -> backlog         (cancelled, shown in backlog)


class SubtaskVerification(BaseModel):
    """Verification configuration for a subtask."""

    type: str = "command"  # Verification type (e.g., "command", "browser", "manual", "code_review", "testing", etc.)
    run: str | None = None  # Command to run (e.g., "npm test")
    scenario: str | None = None  # Browser test scenario


class Subtask(BaseModel):
    """Subtask model from implementation plan."""

    id: str
    title: str
    description: str | None = None
    status: Literal["pending", "in_progress", "completed", "failed", "blocked"] = "pending"
    files: list[str] = Field(default_factory=list)  # Files affected by this subtask
    verification: SubtaskVerification | None = None  # How to verify completion
    # Dependency-graph + timing fields (#94: feeds the cockpit's live execution
    # diagram). Additive + optional — older plans without them serialize as
    # [] / null and the diagram degrades gracefully.
    depends_on: list[str] = Field(default_factory=list)  # IDs of prerequisite subtasks
    service: str | None = None  # Which service (backend/frontend/worker) — diagram accent
    started_at: str | None = None  # ISO; set when the subtask is picked up
    completed_at: str | None = None  # ISO; set when it finishes


class TaskBase(BaseModel):
    """Base task model."""

    title: str = Field(..., description="Task title")
    description: str = Field(..., description="Task description/requirements")


class TaskCreate(TaskBase):
    """Model for creating a new task."""

    project_id: str = Field(..., description="ID of the project this task belongs to")
    metadata: Optional["TaskMetadataUpdate"] = Field(
        None, description="Optional task metadata"
    )


class SelectedSkill(BaseModel):
    """A skill selected to be applied to a task."""

    id: str  # '{category}/{skill_name}'
    name: str  # human-readable display name
    category: str  # parent category
    source: str | None = None  # optional source URL from skill metadata


class TaskMetadata(BaseModel):
    """Task metadata fields."""

    sourceType: str | None = None
    category: str | None = None
    priority: str | None = None
    complexity: str | None = None
    impact: str | None = None
    # GitHub integration
    githubIssueNumber: int | None = None
    affectedFiles: list[str] | None = None
    acceptanceCriteria: list[str] | None = None
    model: str | None = None
    thinkingLevel: str | None = None
    requireReviewBeforeCoding: bool | None = None
    # Execution mode: 'quick' uses simplified prompts (~70% fewer tokens)
    mode: str | None = None  # 'quick' or 'full'
    # Phase-specific model/thinking configuration (Auto profile)
    isAutoProfile: bool | None = None
    phaseModels: dict | None = None
    phaseThinking: dict | None = None
    # Git options
    baseBranch: str | None = None
    # Archive info
    archivedAt: str | None = None
    archivedInVersion: str | None = None
    # Skills attached to this task
    selectedSkills: list[SelectedSkill] | None = None


class Task(TaskBase):
    """Full task model with all fields."""

    id: str = Field(..., description="Unique task ID")
    spec_id: str = Field(..., description="Spec directory name (e.g., '001-feature')")
    project_id: str = Field(..., description="Project ID")
    status: TaskStatus = Field("backlog", description="Current task status")
    phase: str | None = Field(None, description="Current execution phase")
    subtasks: list[Subtask] = Field(default_factory=list)
    created_at: str = Field(..., description="ISO timestamp")
    updated_at: str = Field(..., description="ISO timestamp")
    worktree_path: str | None = Field(
        None, description="Path to git worktree if active"
    )
    branch_name: str | None = Field(None, description="Git branch name")
    metadata: TaskMetadata | None = Field(None, description="Task metadata")
    review_reason: str | None = Field(
        None, description="Reason for human review (e.g., 'plan_review')"
    )


class TaskList(BaseModel):
    """Response model for listing tasks."""

    tasks: list[Task]
    total: int


class TaskMetadataUpdate(BaseModel):
    """Model for updating task metadata fields.

    Fields can be set to None to explicitly clear them from the task.
    When a field is not provided (excluded from the request), it won't be modified.
    When a field is set to null/None, it will be removed from the task metadata.
    """

    model: str | None = None
    thinkingLevel: str | None = None
    requireReviewBeforeCoding: bool | None = None
    category: str | None = None
    priority: str | None = None
    complexity: str | None = None
    impact: str | None = None
    # Phase-specific model/thinking configuration (Auto profile)
    isAutoProfile: bool | None = None
    phaseModels: dict | None = None  # {"spec": "sonnet", "planning": "opus", ...}
    phaseThinking: dict | None = None  # {"spec": "medium", "planning": "high", ...}
    # Git options
    baseBranch: str | None = None
    # Execution mode: 'quick' uses simplified prompts (~70% fewer tokens)
    mode: str | None = None  # 'quick' or 'full'
    # Image attachments (can be null to clear)
    attachedImages: list | None = None
    # Referenced files (can be null to clear)
    referencedFiles: list | None = None
    # Skills attached to this task (can be null to clear)
    selectedSkills: list[SelectedSkill] | None = None


class TaskUpdate(BaseModel):
    """Model for updating task fields."""

    title: str | None = None
    description: str | None = None
    status: TaskStatus | None = None
    metadata: TaskMetadataUpdate | None = None


class ClarificationQuestion(BaseModel):
    """A single clarification question with multiple-choice options."""

    id: str
    question: str
    options: list[str] = Field(default_factory=list)


class ClarificationResponse(BaseModel):
    """Response from clarification question generation."""

    questions: list[ClarificationQuestion] = Field(default_factory=list)
    skip: bool = False
    skip_reason: str = Field("", alias="skipReason")

    model_config = {"populate_by_name": True}


class ClarificationAnswer(BaseModel):
    """A single answered clarification question."""

    question_id: str = Field(..., alias="questionId")
    question: str
    answer: str

    model_config = {"populate_by_name": True}


class ClarificationAnswersRequest(BaseModel):
    """Request to submit clarification answers."""

    answers: list[ClarificationAnswer]


# --------------------------------------------------------------------------
# Helper Functions
# --------------------------------------------------------------------------


def get_spec_dirs(project_path: Path) -> list[Path]:
    """Get all spec directories in a project."""
    specs_dir = project_path / ".aifactory" / "specs"
    if not specs_dir.exists():
        return []
    return sorted([d for d in specs_dir.iterdir() if d.is_dir()])


def get_next_spec_id(project_path: Path, title: str) -> str:
    """Generate the next spec ID (e.g., '003-feature-name').

    Uses a counter file (.aifactory/specs/.counter) to ensure IDs
    never get reused after deletion.
    """
    specs_dir = project_path / ".aifactory" / "specs"
    counter_file = specs_dir / ".counter"

    # Read persisted counter (highest ID ever assigned)
    persisted_max = 0
    if counter_file.exists():
        try:
            persisted_max = int(counter_file.read_text().strip())
        except (ValueError, OSError):
            pass

    # Also check existing directories in case counter file is missing
    existing = get_spec_dirs(project_path)
    dir_max = 0
    for spec_dir in existing:
        match = re.match(r"(\d+)-", spec_dir.name)
        if match:
            dir_max = max(dir_max, int(match.group(1)))

    # Use the higher of persisted counter and directory scan
    max_num = max(persisted_max, dir_max)
    next_num = max_num + 1

    # Persist the new counter
    specs_dir.mkdir(parents=True, exist_ok=True)
    counter_file.write_text(str(next_num))

    # Generate slug from title
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:30]

    # Fallback to "untitled-task" if slug is empty
    if not slug:
        slug = "untitled-task"

    return f"{next_num:03d}-{slug}"


def get_worktree_spec_dir(project_path: Path, spec_id: str) -> Path | None:
    """Get the worktree spec directory if it exists.

    Worktree layout: .aifactory/worktrees/tasks/{spec_id}/.aifactory/specs/{spec_id}/
    """
    worktree_spec_dir = (
        project_path
        / ".aifactory"
        / "worktrees"
        / "tasks"
        / spec_id
        / ".aifactory"
        / "specs"
        / spec_id
    )
    if worktree_spec_dir.exists():
        return worktree_spec_dir
    return None


def sync_worktree_to_main_spec(project_path: Path, spec_id: str) -> bool:
    """Sync implementation_plan.json from worktree to main spec if worktree has newer data.

    Returns True if sync was performed, False otherwise.
    """
    main_spec_dir = project_path / ".aifactory" / "specs" / spec_id
    worktree_spec_dir = get_worktree_spec_dir(project_path, spec_id)

    if not worktree_spec_dir:
        return False

    worktree_plan_file = worktree_spec_dir / "implementation_plan.json"
    main_plan_file = main_spec_dir / "implementation_plan.json"

    if not worktree_plan_file.exists():
        return False

    try:
        worktree_plan = json.loads(worktree_plan_file.read_text())
        main_plan = {}
        if main_plan_file.exists():
            main_plan = json.loads(main_plan_file.read_text())

        # Count completed subtasks in each plan
        def count_completed(plan: dict) -> int:
            count = 0
            for phase in plan.get("phases", []):
                for subtask in phase.get("subtasks", []):
                    if subtask.get("status") == "completed":
                        count += 1
            return count

        worktree_completed = count_completed(worktree_plan)
        main_completed = count_completed(main_plan)

        # Only sync if worktree has more progress (more completed subtasks)
        if worktree_completed > main_completed:
            import logging

            logger = logging.getLogger(__name__)
            logger.info(
                f"[WorktreeSync] Syncing plan for {spec_id}: "
                f"worktree has {worktree_completed} completed vs main {main_completed}"
            )
            # Issue #259: control-plane state lives in task_control.json, NOT in
            # the agent-written plan. Strip status/reviewReason from the worktree
            # copy so a sync can never reset the human/system control decision.
            task_control.strip_control_fields(worktree_plan)
            main_plan_file.write_text(json.dumps(worktree_plan, indent=2))
            return True

        return False
    except (json.JSONDecodeError, OSError) as e:
        import logging

        logging.getLogger(__name__).warning(
            f"[WorktreeSync] Failed to sync {spec_id}: {e}"
        )
        return False


def validate_done_status(plan: dict) -> tuple[bool, str]:
    """Validate that all subtasks are completed before allowing 'done' status.

    Returns (is_valid, error_message).
    """
    phases = plan.get("phases", [])
    if not phases:
        # No phases means no subtasks to validate
        return True, ""

    total_subtasks = 0
    completed_subtasks = 0

    for phase in phases:
        for subtask in phase.get("subtasks", []):
            total_subtasks += 1
            if subtask.get("status") == "completed":
                completed_subtasks += 1

    if total_subtasks == 0:
        return True, ""

    if completed_subtasks < total_subtasks:
        return False, (
            f"Cannot mark as done: only {completed_subtasks}/{total_subtasks} "
            f"subtasks are completed. Complete all subtasks first or check if "
            f"worktree has newer progress."
        )

    return True, ""


def get_plan_with_worktree_sync(project_path: Path, spec_id: str) -> tuple[dict, Path]:
    """Get implementation plan, syncing from worktree first if needed.

    Returns (plan_dict, plan_file_path).
    """
    # Sync worktree to main spec first
    sync_worktree_to_main_spec(project_path, spec_id)

    # Read from main spec (now potentially updated)
    main_spec_dir = project_path / ".aifactory" / "specs" / spec_id
    plan_file = main_spec_dir / "implementation_plan.json"

    plan = {}
    if plan_file.exists():
        try:
            plan = json.loads(plan_file.read_text())
        except json.JSONDecodeError:
            pass

    return plan, plan_file


def load_spec_metadata(spec_dir: Path) -> dict:
    """Load metadata for a spec from its files."""
    metadata = {
        "title": spec_dir.name,
        "description": "",
        "status": "backlog",
        "phase": None,
        "subtasks": [],
        "worktree_path": None,
        "branch_name": None,
        "archivedAt": None,
        "archivedInVersion": None,
        "reviewReason": None,
    }

    # Try to load requirements.json for title/description (most accurate source)
    requirements_file = spec_dir / "requirements.json"
    if requirements_file.exists():
        try:
            requirements = json.loads(requirements_file.read_text())
            if "title" in requirements:
                metadata["title"] = requirements["title"]
            if "description" in requirements:
                metadata["description"] = requirements["description"]
        except (json.JSONDecodeError, KeyError):
            pass

    # Fall back to spec.md if requirements.json not available
    if not metadata["description"]:
        spec_file = spec_dir / "spec.md"
        if spec_file.exists():
            content = spec_file.read_text()
            # Extract title from first # heading if not already set
            if not metadata["title"] or metadata["title"] == spec_dir.name:
                title_match = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
                if title_match:
                    metadata["title"] = title_match.group(1)
            # Use first paragraph as description (no truncation)
            paragraphs = re.split(r"\n\n+", content)
            for p in paragraphs[1:]:  # Skip title
                if p.strip() and not p.startswith("#"):
                    metadata["description"] = p.strip()
                    break

    # Try to load task_logs.json for active phase status (most accurate)
    task_logs_file = spec_dir / "task_logs.json"
    if task_logs_file.exists():
        try:
            logs = json.loads(task_logs_file.read_text())
            phases = logs.get("phases", {})

            # First check for any active phase
            has_active_phase = False
            for phase_name, phase_data in phases.items():
                if phase_data.get("status") == "active":
                    metadata["phase"] = phase_name
                    metadata["status"] = "in_progress"
                    has_active_phase = True
                    break

            # If no active phase, check for terminal states
            if not has_active_phase:
                # Check if any phase failed → task needs human intervention
                has_failed_phase = any(
                    phase_data.get("status") == "failed"
                    for phase_data in phases.values()
                )
                if has_failed_phase:
                    metadata["status"] = "human_review"
                    metadata["reviewReason"] = "errors"
                else:
                    # Check validation phase completed (strongest completion signal)
                    validation_phase = phases.get("validation", {})
                    if validation_phase.get(
                        "status"
                    ) == "completed" and validation_phase.get("entries"):
                        metadata["phase"] = "validation"
                        metadata["status"] = "human_review"
                        metadata["reviewReason"] = "completed"
                    else:
                        # Fall back to coding phase completed
                        coding_phase = phases.get("coding", {})
                        if coding_phase.get(
                            "status"
                        ) == "completed" and coding_phase.get("entries"):
                            metadata["phase"] = "coding"
                            metadata["status"] = "human_review"
                            metadata["reviewReason"] = "completed"
        except (json.JSONDecodeError, KeyError):
            pass

    # Try to load implementation_plan.json for status/subtasks
    plan_file = spec_dir / "implementation_plan.json"
    explicit_status = None  # Track if user explicitly set status via kanban
    if plan_file.exists():
        try:
            plan = json.loads(plan_file.read_text())
            # Only set phase from plan if not already set from task_logs
            if not metadata["phase"]:
                metadata["phase"] = plan.get("phase")

            # If no explicit phase, try to detect from phases array
            if not metadata["phase"] and "phases" in plan:
                for phase in plan["phases"]:
                    if isinstance(phase, dict):
                        phase_status = phase.get("status", "")
                        if phase_status == "in_progress":
                            metadata["phase"] = phase.get("name", phase.get("id"))
                            break

            # Check if status was explicitly set (kanban drag-drop saves this)
            # "done" and "completed" statuses ALWAYS take precedence (task was explicitly finished)
            # Other statuses only apply if we didn't already detect active status from task_logs
            if "status" in plan:
                explicit_status = plan["status"]
                if explicit_status in ("done", "completed"):
                    # Task was explicitly marked as done - always honor this
                    metadata["status"] = explicit_status
                elif metadata["status"] == "backlog":
                    # Only override backlog with other statuses
                    metadata["status"] = explicit_status

            # Load reviewReason if present (e.g., 'plan_review')
            if "reviewReason" in plan:
                metadata["reviewReason"] = plan["reviewReason"]

            # Check for qa_signoff.status == "approved" which means task completed QA
            # This should show as human_review for final merge approval
            qa_signoff = plan.get("qa_signoff") or {}
            if (
                qa_signoff.get("status") == "approved"
                and metadata["status"] == "backlog"
            ):
                metadata["status"] = "human_review"
                metadata["reviewReason"] = "completed"

            # Load archive metadata
            if "archivedAt" in plan:
                metadata["archivedAt"] = plan["archivedAt"]
            if "archivedInVersion" in plan:
                metadata["archivedInVersion"] = plan["archivedInVersion"]

            # Load subtasks - can be at top level or nested in phases
            all_subtasks = []

            # First check for top-level subtasks (legacy format).
            # Tolerate both list shape (canonical) and dict shape
            # (partial-sync artifact from agent_service that maps
            # subtask_id -> {status, notes, ...}).  Without this guard,
            # iterating a dict yields the keys as strings and the loop
            # at the bottom blows up with AttributeError on st.get(...).
            if "subtasks" in plan:
                raw_subtasks = plan["subtasks"]
                if isinstance(raw_subtasks, list):
                    all_subtasks.extend(raw_subtasks)
                elif isinstance(raw_subtasks, dict):
                    for sid, st in raw_subtasks.items():
                        if isinstance(st, dict):
                            st_copy = dict(st)
                            st_copy.setdefault("id", sid)
                            all_subtasks.append(st_copy)

            # Then check for subtasks nested in phases (current format)
            if "phases" in plan:
                for phase in plan["phases"]:
                    if isinstance(phase, dict) and "subtasks" in phase:
                        phase_name = phase.get("name", "")
                        for st in phase["subtasks"]:
                            # Prefix subtask with phase name for clarity
                            st_copy = st.copy() if isinstance(st, dict) else {}
                            if phase_name and "title" not in st_copy:
                                st_copy["title"] = st_copy.get("description", "Subtask")
                            all_subtasks.append(st_copy)

            if all_subtasks:
                metadata["subtasks"] = []
                for i, st in enumerate(all_subtasks):
                    # Build files list from 'file' (single) or 'files'
                    # (array) fields.  Tolerate three shapes the planner
                    # has been observed to emit:
                    #   files: "path/to/x.py"                  (str)
                    #   files: ["a.py", "b.py"]                (list[str])
                    #   files: {"create": ["a.py"], "modify": ["b.py"]}
                    #     (dict — happens when the planner groups files
                    #     by intent; flatten the values into a single
                    #     list of strings)
                    files = []
                    if st.get("file"):
                        files.append(st["file"])
                    raw_files = st.get("files")
                    if isinstance(raw_files, str):
                        files.append(raw_files)
                    elif isinstance(raw_files, list):
                        files.extend(f for f in raw_files if isinstance(f, str))
                    elif isinstance(raw_files, dict):
                        for v in raw_files.values():
                            if isinstance(v, list):
                                files.extend(f for f in v if isinstance(f, str))
                            elif isinstance(v, str):
                                files.append(v)

                    # Build verification from 'verification' or 'verification_method' fields
                    verification = None
                    if st.get("verification"):
                        v = st["verification"]
                        if isinstance(v, dict):
                            verification = SubtaskVerification(
                                type=v.get("type", "command"),
                                run=v.get("run") or v.get("command"),
                                scenario=v.get("scenario"),
                            )
                        elif isinstance(v, str):
                            # Simple string verification becomes a command
                            verification = SubtaskVerification(type="command", run=v)
                    elif st.get("verification_method"):
                        verification = SubtaskVerification(
                            type="command", run=st["verification_method"]
                        )

                    # Dependency edges + timing for the live diagram (#94). The
                    # planner persists these via the implementation_plan Subtask
                    # dataclass; tolerate absence (defaults keep old plans valid).
                    depends_on = st.get("depends_on")
                    if not isinstance(depends_on, list):
                        depends_on = []
                    depends_on = [d for d in depends_on if isinstance(d, str)]

                    metadata["subtasks"].append(
                        Subtask(
                            id=st.get("id", str(i)),
                            title=st.get("title")
                            or st.get("description", f"Subtask {i + 1}")[:80],
                            description=st.get("description") or st.get("notes"),
                            status=st.get("status", "pending"),
                            files=files,
                            verification=verification,
                            depends_on=depends_on,
                            service=st.get("service"),
                            started_at=st.get("started_at"),
                            completed_at=st.get("completed_at"),
                        )
                    )
        except (json.JSONDecodeError, KeyError):
            pass

    # Check for worktree
    worktree_marker = spec_dir / ".worktree_path"
    if worktree_marker.exists():
        metadata["worktree_path"] = worktree_marker.read_text().strip()
        metadata["branch_name"] = f"aifactory/{spec_dir.name}"

    # Load task metadata from requirements.json
    requirements_file = spec_dir / "requirements.json"
    if requirements_file.exists():
        try:
            requirements = json.loads(requirements_file.read_text())
            metadata["task_metadata"] = requirements.get("metadata", {})
        except (json.JSONDecodeError, KeyError):
            metadata["task_metadata"] = {}
    else:
        metadata["task_metadata"] = {}

    # Detect status from subtask progress if not already set
    # If any subtasks are completed but not all done, task is in_progress
    if metadata["status"] == "backlog" and metadata.get("subtasks"):
        subtasks = metadata["subtasks"]
        completed_count = sum(1 for st in subtasks if st.status == "completed")
        in_progress_count = sum(1 for st in subtasks if st.status == "in_progress")
        if completed_count > 0 and completed_count < len(subtasks):
            # Work has been done but not finished
            metadata["status"] = "in_progress"
            metadata["phase"] = "coding"
        elif in_progress_count > 0:
            # Currently working on subtasks
            metadata["status"] = "in_progress"
            metadata["phase"] = "coding"
        elif completed_count == len(subtasks) and len(subtasks) > 0:
            # All subtasks completed - needs review
            metadata["status"] = "human_review"
            metadata["reviewReason"] = "completed"

    # Correctness guard: a phase can log completed even when subtasks failed
    # or the build halted early. Never report a clean completed review state
    # then -- surface that it needs attention so the portal never shows a
    # halted/failed build as Completed. User-set done/completed wins below.
    if explicit_status not in ("done", "completed"):
        _subs = metadata.get("subtasks") or []
        if _subs:
            _n_failed = sum(
                1 for st in _subs if getattr(st, "status", None) == "failed"
            )
            _n_done = sum(
                1 for st in _subs if getattr(st, "status", None) == "completed"
            )
            if _n_failed:
                metadata["status"] = "human_review"
                metadata["reviewReason"] = "errors"
            elif metadata.get("reviewReason") == "completed" and _n_done < len(_subs):
                metadata["status"] = "human_review"
                metadata["reviewReason"] = "incomplete"

    # Final safety: "done"/"completed" always wins over all auto-detection
    # This guards against task_logs or subtask detection overriding user intent
    if explicit_status in ("done", "completed"):
        metadata["status"] = explicit_status

    # Only use file-based status detection if no explicit status was set via kanban
    # AND status wasn't already determined from task_logs.json (coding completed)
    # This allows users to override status via drag-and-drop
    if explicit_status is None and metadata["status"] == "backlog":
        if (spec_dir / "QA_FIX_REQUEST.md").exists():
            metadata["status"] = "human_review"
            metadata["reviewReason"] = "qa_rejected"
        elif (spec_dir / "qa_report.md").exists():
            report = (spec_dir / "qa_report.md").read_text()
            if "PASSED" in report.upper():
                metadata["status"] = "human_review"
                metadata["reviewReason"] = "completed"
            elif "FAILED" in report.upper():
                metadata["status"] = "human_review"
                metadata["reviewReason"] = "qa_rejected"
            else:
                metadata["status"] = "ai_review"  # QA still in progress
        elif metadata["phase"]:
            metadata["status"] = "in_progress"

    # Control-plane override (Issue #259): the dedicated control store is the
    # authoritative source for board column / status and reviewReason. It is
    # written ONLY by the web-server and is never touched by worktree sync, so
    # a human/system control decision here wins over anything derived above
    # from the agent-written implementation_plan.json / task_logs.json.
    #
    # When the store is absent it falls back (read-time) to any status/
    # reviewReason still living in implementation_plan.json, so pre-#259 specs
    # behave exactly as before.
    control = task_control.read_control(spec_dir)
    if control.get("status"):
        metadata["status"] = control["status"]
        # reviewReason is owned alongside status: take the store's value
        # (which may be absent, meaning "no review reason").
        metadata["reviewReason"] = control.get("reviewReason")

    return metadata


def spec_to_task(project_id: str, spec_dir: Path) -> Task:
    """Convert a spec directory to a Task model."""
    metadata = load_spec_metadata(spec_dir)

    # Get timestamps from directory
    stat = spec_dir.stat()

    # Map backend status to frontend-compatible status
    frontend_status = map_backend_status_to_frontend(metadata["status"])

    # Build task metadata if available
    task_metadata = None
    if metadata.get("task_metadata"):
        task_metadata = TaskMetadata(**metadata["task_metadata"])

    return Task(
        id=f"{project_id}:{spec_dir.name}",
        spec_id=spec_dir.name,
        project_id=project_id,
        title=metadata["title"],
        description=metadata["description"],
        status=frontend_status,
        phase=metadata["phase"],
        subtasks=metadata["subtasks"],
        created_at=datetime.fromtimestamp(stat.st_ctime).isoformat(),
        updated_at=datetime.fromtimestamp(stat.st_mtime).isoformat(),
        worktree_path=metadata["worktree_path"],
        branch_name=metadata["branch_name"],
        metadata=task_metadata,
        review_reason=metadata.get("reviewReason"),
    )


def map_backend_status_to_frontend(backend_status: str) -> str:
    """Map backend task status to frontend-compatible status.

    Backend statuses: backlog, planning, in_progress, review, qa_pending, qa_failed, completed, cancelled
    Frontend statuses: backlog, in_progress, ai_review, human_review, done
    """
    status_mapping = {
        # Backend statuses -> frontend statuses
        "backlog": "backlog",
        "planning": "backlog",  # Planning tasks go in backlog column
        "in_progress": "in_progress",
        "review": "human_review",  # Build ready for review/merge - needs human action
        "qa_pending": "ai_review",
        "qa_failed": "human_review",  # Failed QA needs human attention
        "completed": "human_review",  # Completed tasks need merge approval
        "cancelled": "backlog",  # Cancelled tasks shown in backlog (could be hidden later)
        # Frontend statuses (pass through when already mapped or set via kanban)
        "ai_review": "ai_review",
        "human_review": "human_review",
        "done": "done",
    }
    return status_mapping.get(backend_status, "backlog")


def get_execution_progress(spec_dir: Path, subtasks: list) -> dict | None:
    """Compute execution progress from task_logs.json and subtasks.

    Returns ExecutionProgress dict or None if not available.
    """
    # Also check worktree for task_logs.json
    project_path = spec_dir.parent.parent  # .aifactory/specs -> project root
    worktree_spec_dir = (
        project_path
        / "worktrees"
        / "tasks"
        / spec_dir.name
        / ".aifactory"
        / "specs"
        / spec_dir.name
    )

    task_logs_file = None
    for check_dir in [worktree_spec_dir, spec_dir]:
        candidate = check_dir / "task_logs.json"
        if candidate.exists():
            task_logs_file = candidate
            break

    if not task_logs_file:
        return None

    try:
        task_logs = json.loads(task_logs_file.read_text())
        phases = task_logs.get("phases", {})

        # Determine current phase from task_logs status
        # Maps task_logs.json phase names to frontend ExecutionPhase values
        phase_map = {
            "planning": "planning",
            "plan_review": "plan_review",
            "coding": "coding",
            "validation": "qa_review",
            "qa_review": "qa_review",
            "qa_fixing": "qa_fixing",
            "complete": "complete",
            "failed": "failed",
        }

        # Phase order for progress calculation
        phase_order = ["planning", "plan_review", "coding", "validation", "qa_fixing"]
        phase_weights = {
            "planning": 10,
            "plan_review": 5,
            "coding": 60,
            "validation": 15,
            "qa_fixing": 10,
        }  # % of total progress

        current_phase = "idle"
        current_phase_key = None
        started_at = None
        phase_progress = 0

        for log_phase, log_data in phases.items():
            # Get earliest started_at from any phase
            if log_data.get("started_at") and not started_at:
                started_at = log_data["started_at"]
            elif log_data.get("started_at") and started_at:
                # Keep the earliest timestamp
                if log_data["started_at"] < started_at:
                    started_at = log_data["started_at"]

            if log_data.get("status") == "active":
                current_phase = phase_map.get(log_phase, log_phase)
                current_phase_key = log_phase

        # If no active phase, check for terminal states (completed/failed)
        if current_phase == "idle" and phases:
            has_failed = any(p.get("status") == "failed" for p in phases.values())
            has_completed = any(p.get("status") == "completed" for p in phases.values())

            if has_failed:
                current_phase = "failed"
            elif has_completed:
                validation = phases.get("validation", {})
                coding = phases.get("coding", {})
                if validation.get("status") == "completed":
                    current_phase = "complete"
                elif coding.get("status") == "completed":
                    current_phase = "complete"

        # Calculate overall progress from subtasks
        completed = sum(1 for s in subtasks if s.status == "completed")
        total = len(subtasks)
        overall_progress = int((completed / total) * 100) if total > 0 else 0

        # Override progress for terminal states
        if current_phase in ("complete", "failed"):
            phase_progress = 100
            overall_progress = 100

        # Calculate phase-specific progress
        if current_phase_key:
            phase_data = phases.get(current_phase_key, {})
            entries = phase_data.get("entries", [])
            # Estimate phase progress based on entries (simple heuristic)
            if entries:
                # Count completed tools vs total activity
                tool_starts = sum(1 for e in entries if e.get("type") == "tool_start")
                tool_ends = sum(1 for e in entries if e.get("type") == "tool_end")
                if tool_starts > 0:
                    phase_progress = min(100, int((tool_ends / tool_starts) * 100))
                else:
                    phase_progress = 50  # Activity detected but no tools tracked
            else:
                phase_progress = 10  # Phase started but no entries yet

        # Find current subtask
        current_subtask = None
        for s in subtasks:
            if s.status == "in_progress":
                current_subtask = s.title
                break

        # Generate sequence number from file modification time for stale update detection
        sequence_number = int(task_logs_file.stat().st_mtime * 1000)

        return {
            "phase": current_phase,
            "phaseProgress": phase_progress,
            "overallProgress": overall_progress,
            "currentSubtask": current_subtask,
            "message": f"{completed}/{total} subtasks completed",
            "startedAt": started_at,
            "sequenceNumber": sequence_number,
        }
    except (json.JSONDecodeError, Exception):
        return None


def task_to_dict(task: Task) -> dict:
    """Convert a Task model to a dict with camelCase keys for frontend."""
    # Get execution progress and archive metadata if task has a spec directory
    execution_progress = None
    archive_metadata = {}
    specs_path = None
    if task.spec_id:
        # Try to find spec dir for this task
        projects = load_projects()
        if task.project_id in projects:
            project_path = Path(projects[task.project_id]["path"])
            spec_dir = project_path / ".aifactory" / "specs" / task.spec_id
            if spec_dir.exists():
                specs_path = str(spec_dir)  # Store path for frontend Files tab
                execution_progress = get_execution_progress(spec_dir, task.subtasks)
                # Load archive metadata from plan file
                plan_file = spec_dir / "implementation_plan.json"
                if plan_file.exists():
                    try:
                        plan = json.loads(plan_file.read_text())
                        if "archivedAt" in plan:
                            archive_metadata["archivedAt"] = plan["archivedAt"]
                        if "archivedInVersion" in plan:
                            archive_metadata["archivedInVersion"] = plan[
                                "archivedInVersion"
                            ]
                    except json.JSONDecodeError:
                        pass

    result = {
        "id": task.id,
        "specId": task.spec_id,
        "projectId": task.project_id,
        "title": task.title,
        "description": task.description,
        "status": map_backend_status_to_frontend(task.status),
        "phase": task.phase,
        "subtasks": [
            {
                "id": s.id,
                "title": s.title,
                "description": s.description,
                "status": s.status,
                "files": s.files,
                "verification": {
                    "type": s.verification.type,
                    "run": s.verification.run,
                    "scenario": s.verification.scenario,
                }
                if s.verification
                else None,
                # Dependency graph + timing for the cockpit's live diagram (#94).
                "depends_on": getattr(s, "depends_on", []),
                "service": getattr(s, "service", None),
                "started_at": getattr(s, "started_at", None),
                "completed_at": getattr(s, "completed_at", None),
            }
            for s in task.subtasks
        ],
        "logs": [],  # Required by frontend Task interface
        "createdAt": task.created_at,
        "updatedAt": task.updated_at,
        "worktreePath": task.worktree_path,
        "branchName": task.branch_name,
        "reviewReason": task.review_reason,
        "specsPath": specs_path,  # Path to spec directory for Files tab
    }

    if execution_progress:
        result["executionProgress"] = execution_progress

    # Include task metadata (settings from requirements.json)
    metadata_payload = (
        task.metadata.model_dump(exclude_none=True) if task.metadata else {}
    )
    if archive_metadata:
        metadata_payload.update(archive_metadata)  # Add archive info if any
    if metadata_payload:
        result["metadata"] = metadata_payload

    return result


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


def _pfactory_priority_rank(spec_dir: Path) -> int:
    """PFactory priority sort rank for a spec (p0 → first); 99 when not a
    prioritised PFactory spec. Best-effort: any failure sorts the task last.
    See epic #327 / #331.
    """
    import sys

    backend_path = Path(__file__).parent.parent.parent.parent / "backend"
    if str(backend_path) not in sys.path:
        sys.path.insert(0, str(backend_path))
    try:
        from pfactory.routing import priority_rank
        from pfactory.taxonomy import classify_requirements

        req_file = spec_dir / "requirements.json"
        if req_file.exists():
            req = json.loads(req_file.read_text())
            return priority_rank(classify_requirements(req).priority)
    except (json.JSONDecodeError, OSError, ImportError):
        pass
    return 99


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
        spec_dirs = get_spec_dirs(project_path)
        for spec_dir in spec_dirs:
            task = spec_to_task(pid, spec_dir)
            if status is None or task.status == status:
                all_tasks.append(task)
                priority_ranks[task.id] = _pfactory_priority_rank(spec_dir)

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


def _resolve_task(task_id: str) -> tuple[str, str, Path, Path]:
    """Resolve task_id (projectId:specId) to project_id, spec_id, project_path, spec_dir.

    Raises HTTPException on invalid input or missing resources.
    """
    if ":" not in task_id:
        raise HTTPException(
            status_code=400, detail="Invalid task_id format (expected projectId:specId)"
        )

    project_id, spec_id = task_id.split(":", 1)
    projects = load_projects()

    if project_id not in projects:
        raise HTTPException(status_code=404, detail="Project not found")

    project_path = Path(projects[project_id]["path"])
    spec_dir = project_path / ".aifactory" / "specs" / spec_id

    if not spec_dir.exists():
        raise HTTPException(status_code=404, detail="Task spec not found")

    return project_id, spec_id, project_path, spec_dir


@router.get("/{task_id}/token-usage")
async def get_task_token_usage(
    task_id: str, _access: dict = Depends(require_task_access("viewer"))
):
    """Per-category token / cost breakdown for a task's session(s) (#262).

    Returns the structured breakdown produced by the backend token-attribution
    module: each source category (system/CLAUDE.md instructions, user messages,
    team/coordination context, tool outputs, thinking+output) with its token
    count, %-of-context-window and apportioned $ cost, plus session totals.

    Reads the agent-written ``token_usage.json`` from the main spec dir (the
    agent loop syncs it back from the worktree). Returns an empty (all-zero)
    breakdown when no session has run yet — never 404 on a valid task, so the
    UI can render a stable empty state.
    """
    project_id, spec_id, project_path, spec_dir = _resolve_task(task_id)

    # Prefer the main spec dir (synced from worktree). Fall back to the live
    # worktree spec dir if the sync hasn't landed yet.
    candidate = spec_dir / "token_usage.json"
    if not candidate.exists():
        worktree_spec_dir = get_worktree_spec_dir(project_path, spec_id)
        if worktree_spec_dir and (worktree_spec_dir / "token_usage.json").exists():
            spec_dir = worktree_spec_dir

    # Import the backend attribution reader (sys.path shim, same approach as
    # reject_plan above — web-server doesn't always have backend on PYTHONPATH).
    import sys

    backend_path = Path(__file__).parent.parent.parent.parent / "backend"
    if str(backend_path) not in sys.path:
        sys.path.insert(0, str(backend_path))

    try:
        from agents.token_attribution import read_breakdown
    except ImportError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Token attribution module unavailable: {exc}",
        ) from exc

    return read_breakdown(spec_dir)


def _not_running_resource_usage() -> dict:
    """The point-in-time resource shape when no agent process is active.

    Used both when the task has no live subprocess and as the degraded
    fallback whenever sampling fails (dead/missing PID, psutil error, etc.).
    """
    return {
        "running": False,
        "pid": None,
        "cpuPercent": 0.0,
        "memoryMb": 0.0,
        "memoryPercent": 0.0,
        "sampledAt": datetime.now(timezone.utc).isoformat(),
    }


def _sample_process_resources(pid: int) -> dict:
    """Sample CPU%/RAM for ``pid`` (and its children) using psutil.

    Returns the populated resource shape, or the not-running shape if the
    PID is gone or sampling raises for any reason. Never propagates an
    exception — the endpoint must degrade gracefully, not 500.

    CPU% is measured with a short blocking ``interval`` so a single
    point-in-time poll yields a meaningful number (psutil's non-blocking
    mode returns 0.0 on the first call for a process it hasn't seen). The
    parent's and children's percentages are summed so multi-process agent
    runs (e.g. a CLI that spawns workers) report aggregate load.
    """
    try:
        import psutil
    except ImportError:
        # psutil not installed — degrade rather than crash the endpoint.
        return _not_running_resource_usage()

    try:
        proc = psutil.Process(pid)

        # Gather parent + children once so we can sum CPU and RSS.
        procs = [proc]
        try:
            procs.extend(proc.children(recursive=True))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

        # Prime CPU counters, then sample over a short interval. Priming on
        # the parent is enough for it; children are primed implicitly by the
        # first read and summed best-effort (0.0 on their first read is
        # acceptable for a point-in-time poll the frontend repeats).
        cpu_percent = 0.0
        memory_mb = 0.0
        for p in procs:
            try:
                if p.pid == pid:
                    cpu_percent += p.cpu_percent(interval=0.1)
                else:
                    cpu_percent += p.cpu_percent(interval=None)
                memory_mb += p.memory_info().rss / (1024 * 1024)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                # A child died mid-sample; skip it.
                continue

        # System RAM percentage from the aggregate RSS.
        try:
            total_ram = psutil.virtual_memory().total
            memory_percent = (
                (memory_mb * 1024 * 1024 / total_ram) * 100 if total_ram else 0.0
            )
        except Exception:
            memory_percent = 0.0

        return {
            "running": True,
            "pid": pid,
            "cpuPercent": round(cpu_percent, 1),
            "memoryMb": round(memory_mb, 1),
            "memoryPercent": round(memory_percent, 2),
            "sampledAt": datetime.now(timezone.utc).isoformat(),
        }
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        # PID gone or inaccessible between the running-check and the sample.
        return _not_running_resource_usage()
    except Exception:
        # Belt-and-braces: any unexpected psutil/OS error degrades cleanly.
        return _not_running_resource_usage()


@router.get("/{task_id}/resource-usage")
async def get_task_resource_usage(
    task_id: str, _access: dict = Depends(require_task_access("viewer"))
):
    """Point-in-time CPU/RAM of the running agent subprocess for a task (#277).

    The frontend polls this to drive a live per-agent resource panel. Returns
    raw JSON (the api-client wraps it):

        {
          "running": bool,        # is an agent process active for this task
          "pid": int | null,
          "cpuPercent": number,   # process CPU% (parent + children), 0.0 when idle
          "memoryMb": number,     # aggregate RSS in MB
          "memoryPercent": number,# % of system RAM
          "sampledAt": str        # ISO-8601 UTC
        }

    Behaviour:
      - Unknown task → 404 (via ``_resolve_task``).
      - Valid task with no live process → the not-running shape (never 404).
      - Sampling is failure-safe: a dead/missing PID or psutil error degrades
        to the not-running shape rather than raising.
    """
    # 404 only for an unknown task (bad format / missing project / missing spec).
    _resolve_task(task_id)

    from ..services.agent_service import get_agent_service

    agent_service = get_agent_service()

    # running_tasks maps task_id -> asyncio.subprocess.Process; .pid is the
    # OS pid of the spawned agent CLI. Absence means no live agent process.
    proc = agent_service.running_tasks.get(task_id)
    if proc is None or proc.pid is None:
        return _not_running_resource_usage()

    # If the process object exists but has already exited, returncode is set —
    # treat that as not-running too rather than sampling a reaped pid.
    if getattr(proc, "returncode", None) is not None:
        return _not_running_resource_usage()

    return _sample_process_resources(proc.pid)


@router.post("/{task_id}/clarifications", response_model=ClarificationResponse)
async def generate_clarifications(
    task_id: str, _access: dict = Depends(require_task_access("member"))
):
    """Generate clarification questions for a task using an LLM."""
    from ..services.clarification_service import generate_clarification_questions

    project_id, spec_id, project_path, spec_dir = _resolve_task(task_id)

    # Load task title and description from requirements.json
    req_file = spec_dir / "requirements.json"
    if not req_file.exists():
        return ClarificationResponse(skip=True, skipReason="No requirements found.")

    requirements = json.loads(req_file.read_text())
    title = requirements.get("title", "")
    description = requirements.get("description", "")

    result = await generate_clarification_questions(title, description, project_path)

    return ClarificationResponse(
        questions=[ClarificationQuestion(**q) for q in result.get("questions", [])],
        skip=result.get("skip", False),
        skipReason=result.get("skipReason", ""),
    )


@router.post("/{task_id}/clarifications/answers", response_model=Task)
async def submit_clarification_answers(
    task_id: str,
    request: ClarificationAnswersRequest,
    _access: dict = Depends(require_task_access("member")),
):
    """Submit answers to clarification questions and append them to the task."""
    project_id, spec_id, project_path, spec_dir = _resolve_task(task_id)

    if not request.answers:
        return spec_to_task(project_id, spec_dir)

    # Build clarification appendix
    lines = ["\n\n## Clarifications\n"]
    for answer in request.answers:
        if answer.answer.strip():
            lines.append(f"**Q: {answer.question}**")
            lines.append(f"A: {answer.answer.strip()}\n")
    appendix = "\n".join(lines)

    # Update requirements.json description
    req_file = spec_dir / "requirements.json"
    if req_file.exists():
        requirements = json.loads(req_file.read_text())
        requirements["description"] = requirements.get("description", "") + appendix
        req_file.write_text(json.dumps(requirements, indent=2))

    # Append to spec.md
    spec_file = spec_dir / "spec.md"
    if spec_file.exists():
        content = spec_file.read_text()
        # Insert before ## Notes section if it exists, otherwise append
        if "\n## Notes\n" in content:
            content = content.replace("\n## Notes\n", f"{appendix}\n## Notes\n")
        else:
            content += appendix
        spec_file.write_text(content)

    return spec_to_task(project_id, spec_dir)


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


@router.get("/{task_id}/qa-report")
async def get_qa_report(
    task_id: str, _access: dict = Depends(require_task_access("viewer"))
):
    """Return the QA report markdown for a task.

    Tasks that have completed the QA phase have a ``qa_report.md`` written
    to their spec dir. This endpoint surfaces that content + a few derived
    fields so an MCP client can show it inline without separately reading
    the filesystem.
    """
    if ":" not in task_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid task ID format"
        )

    project_id, spec_id = task_id.split(":", 1)
    projects = load_projects()
    if project_id not in projects:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Project not found"
        )

    project_path = Path(projects[project_id]["path"])
    spec_dir = project_path / ".aifactory" / "specs" / spec_id
    qa_report_file = spec_dir / "qa_report.md"

    if not qa_report_file.exists():
        # 404 is the right answer — clients should treat "no report yet"
        # as "task hasn't reached QA" rather than a hard error.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="QA report not found — task may not have reached the QA phase yet",
        )

    try:
        content = qa_report_file.read_text(encoding="utf-8")
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Could not read QA report: {exc}",
        ) from exc

    return {
        "task_id": task_id,
        "spec_id": spec_id,
        "exists": True,
        "size_bytes": qa_report_file.stat().st_size,
        "modified_at": qa_report_file.stat().st_mtime,
        "content": content,
    }


@router.get("/{task_id}/agent-console/sse")
async def stream_agent_console(
    task_id: str, _access: dict = Depends(require_task_access("viewer"))
):
    """Server-Sent Events stream of the running agent's console output.

    V1.1 strategy: read ``build-progress.txt`` from the spec dir and emit
    deltas as they appear. This is the same file the portal's progress
    sidebar polls — it covers the 80% case (the user wants to *watch* an
    agent without needing the rmux pane).

    The richer rmux-driven SSE re-broadcast (which would let an MCP client
    drive a live terminal) is a follow-up — it depends on the rmux bridge
    being enabled, which isn't a given on all deployments. The poll-based
    fallback here works regardless.

    Client behaviour: subscribe to the stream, receive ``data:`` events,
    detect ``event: done`` when the agent finishes.
    """
    if ":" not in task_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid task ID format"
        )

    project_id, spec_id = task_id.split(":", 1)
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

    progress_file = spec_dir / "build-progress.txt"

    async def event_generator():
        """Yield SSE-formatted deltas from build-progress.txt.

        Sleeps 1s between polls. Emits an ``event: done`` line + closes
        when the file stops growing for 30s (heuristic: agent finished
        or the file isn't being written anymore). Caps total stream
        duration at 30 minutes to avoid leaking connections from
        misbehaving clients.
        """
        import asyncio

        max_duration_s = 30 * 60
        idle_timeout_s = 30
        poll_interval_s = 1.0
        start = asyncio.get_event_loop().time()
        last_size = 0
        last_change = start

        # Emit a kickoff event so the client knows the stream is live
        # even before there's content (useful when the agent hasn't
        # started writing yet).
        yield f"event: open\ndata: {json.dumps({'task_id': task_id, 'spec_id': spec_id})}\n\n"

        try:
            while True:
                now = asyncio.get_event_loop().time()
                if now - start > max_duration_s:
                    yield 'event: done\ndata: {"reason": "max-duration"}\n\n'
                    return

                if progress_file.exists():
                    current_size = progress_file.stat().st_size
                    if current_size > last_size:
                        with progress_file.open("rb") as fh:
                            fh.seek(last_size)
                            chunk = fh.read(current_size - last_size)
                        last_size = current_size
                        last_change = now
                        # SSE data lines: encode each newline as its own
                        # ``data:`` so multi-line chunks render correctly
                        # in standard EventSource clients.
                        text = chunk.decode("utf-8", errors="replace")
                        for line in text.splitlines():
                            yield f"data: {line}\n"
                        yield "\n"  # blank line terminates the event
                    elif now - last_change > idle_timeout_s:
                        yield 'event: done\ndata: {"reason": "idle-timeout"}\n\n'
                        return
                else:
                    # File doesn't exist yet — keep waiting, may appear
                    # once the agent starts writing.
                    if now - last_change > idle_timeout_s:
                        yield 'event: done\ndata: {"reason": "no-progress-file"}\n\n'
                        return

                await asyncio.sleep(poll_interval_s)
        except asyncio.CancelledError:
            # Client disconnected — fastapi cancels the generator.
            return

    from fastapi.responses import StreamingResponse

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.get("/{task_id}/plan-html")
async def get_plan_html(
    task_id: str, _access: dict = Depends(require_task_access("viewer"))
):
    """Generate and return HTML view of the implementation plan.

    Creates a temporary HTML file with nicely formatted plan for review.
    """
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

    # Import HTML generator from backend
    import sys

    backend_path = Path(__file__).parent.parent.parent.parent / "backend"
    if str(backend_path) not in sys.path:
        sys.path.insert(0, str(backend_path))

    try:
        from review.html_generator import generate_html_plan_review

        # Generate HTML file
        html_file = generate_html_plan_review(spec_dir)

        # Return the HTML content
        from fastapi.responses import HTMLResponse

        return HTMLResponse(content=html_file.read_text(), status_code=200)

    except ImportError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"HTML generator not available: {str(e)}",
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to generate plan HTML: {str(e)}",
        )


@router.get("/{task_id}/logs")
async def get_task_logs(
    task_id: str, _access: dict = Depends(require_task_access("viewer"))
):
    """Get logs for a task.

    Returns phase-based logs from task_logs.json if available,
    checking both main spec dir and worktree.
    """
    import logging

    logger = logging.getLogger(__name__)

    logger.info(f"[GetTaskLogs] Called with task_id: {task_id}")

    if ":" not in task_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid task ID format",
        )

    project_id, spec_id = task_id.split(":", 1)
    logger.info(f"[GetTaskLogs] project_id={project_id}, spec_id={spec_id}")

    projects = load_projects()

    if project_id not in projects:
        logger.error(f"[GetTaskLogs] Project not found: {project_id}")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found",
        )

    project_path = Path(projects[project_id]["path"])
    logger.info(f"[GetTaskLogs] project_path: {project_path}")

    spec_dir = project_path / ".aifactory" / "specs" / spec_id
    worktree_spec_dir = (
        project_path
        / ".aifactory"
        / "worktrees"
        / "tasks"
        / spec_id
        / ".aifactory"
        / "specs"
        / spec_id
    )

    logger.info(f"[GetTaskLogs] Checking spec_dir: {spec_dir}")
    logger.info(f"[GetTaskLogs] Checking worktree_spec_dir: {worktree_spec_dir}")

    # Check for task_logs.json (phase-based logs) - prefer worktree if exists
    task_logs_file = None
    for check_dir in [worktree_spec_dir, spec_dir]:
        candidate = check_dir / "task_logs.json"
        logger.info(f"[GetTaskLogs] Checking {candidate}, exists: {candidate.exists()}")
        if candidate.exists():
            task_logs_file = candidate
            logger.info(f"[GetTaskLogs] Found task_logs.json at: {task_logs_file}")
            break

    if task_logs_file:
        try:
            task_logs = json.loads(task_logs_file.read_text())
            logger.info(
                f"[GetTaskLogs] Successfully loaded task_logs.json, has phases: {'phases' in task_logs}"
            )
            result = {
                "specId": task_logs.get("spec_id", spec_id),
                "createdAt": task_logs.get("created_at"),
                "updatedAt": task_logs.get("updated_at"),
                "phases": task_logs.get("phases", {}),
            }

            # Also include build-progress.txt if it exists (detailed human-readable logs)
            for check_dir in [worktree_spec_dir, spec_dir]:
                build_progress = check_dir / "build-progress.txt"
                if build_progress.exists():
                    result["buildProgress"] = build_progress.read_text()
                    break

            logger.info(
                f"[GetTaskLogs] Returning phase-based logs with {len(result.get('phases', {}))} phases"
            )
            return result
        except json.JSONDecodeError as e:
            logger.error(f"[GetTaskLogs] JSON decode error: {e}")
            pass
    else:
        logger.warning(
            "[GetTaskLogs] No task_logs.json found, returning fallback format"
        )

    # Fallback: Collect logs from legacy sources
    logs = []

    # Implementation plan logs
    plan_file = spec_dir / "implementation_plan.json"
    if plan_file.exists():
        try:
            plan = json.loads(plan_file.read_text())
            if "logs" in plan:
                logs.extend(plan["logs"])
        except json.JSONDecodeError:
            pass

    # QA report
    qa_report = spec_dir / "qa_report.md"
    if qa_report.exists():
        logs.append(
            {
                "type": "qa_report",
                "content": qa_report.read_text(),
                "timestamp": datetime.fromtimestamp(
                    qa_report.stat().st_mtime
                ).isoformat(),
            }
        )

    result = {"logs": logs, "total": len(logs)}

    # Include build-progress.txt if it exists
    for check_dir in [worktree_spec_dir, spec_dir]:
        build_progress = check_dir / "build-progress.txt"
        if build_progress.exists():
            result["buildProgress"] = build_progress.read_text()
            break

    return result


@router.post("/{task_id}/logs/watch")
async def watch_task_logs(
    task_id: str, _access: dict = Depends(require_task_access("viewer"))
):
    """
    Start watching task logs (stub endpoint for frontend compatibility).

    Note: Log streaming is handled via WebSocket, this endpoint is a no-op
    that prevents 404 errors in the frontend.
    """
    return {"success": True, "message": "Log watching handled via WebSocket"}


@router.post("/{task_id}/logs/unwatch")
async def unwatch_task_logs(
    task_id: str, _access: dict = Depends(require_task_access("viewer"))
):
    """
    Stop watching task logs (stub endpoint for frontend compatibility).

    Note: Log streaming is handled via WebSocket, this endpoint is a no-op
    that prevents 404 errors in the frontend.
    """
    return {"success": True, "message": "Log unwatching handled via WebSocket"}


# ============================================
# Worktree Merge Routes
# ============================================


class WorktreeMergeOptions(BaseModel):
    noCommit: bool | None = False


class ConflictResolveOptions(BaseModel):
    """Options for conflict resolution."""

    useAI: bool = True
    strategy: str | None = None


@router.get("/{task_id}/worktree/merge-preview")
async def get_worktree_merge_preview(
    task_id: str, _access: dict = Depends(require_task_access("viewer"))
):
    """
    Preview what will happen when merging the worktree.
    Returns conflict info and files that will be merged.
    """
    import subprocess

    # Find the task's spec directory and worktree
    projects_data_dir = get_data_dir()
    projects_file = projects_data_dir / "projects.json"

    if not projects_file.exists():
        return {"success": False, "error": "No projects configured"}

    projects_data = json.loads(projects_file.read_text())

    # Find the task across all projects
    task_info = None
    project_path = None

    # Handle both dict format (id -> project) and list format
    if isinstance(projects_data, dict):
        projects = list(projects_data.values())
    else:
        projects = projects_data

    for project in projects:
        if isinstance(project, str):
            project_path = Path(project)
        else:
            project_path = Path(project.get("path", ""))

        spec_dir = project_path / ".aifactory" / "specs" / task_id

        if spec_dir.exists():
            # Found the task
            impl_plan = spec_dir / "implementation_plan.json"
            if impl_plan.exists():
                task_info = json.loads(impl_plan.read_text())
            break
    else:
        return {"success": False, "error": f"Task {task_id} not found"}

    # Find the worktree
    worktree_path = project_path / ".aifactory" / "worktrees" / "tasks" / task_id

    if not worktree_path.exists():
        return {"success": False, "error": "No worktree found for this task"}

    # Get the branch name from the worktree
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            check=True,
        )
        worktree_branch = result.stdout.strip()
    except subprocess.CalledProcessError:
        return {"success": False, "error": "Could not determine worktree branch"}

    # Get the base branch (usually develop or main)
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=project_path,
            capture_output=True,
            text=True,
            check=True,
        )
        base_branch = result.stdout.strip()
    except subprocess.CalledProcessError:
        base_branch = "develop"

    # Get list of changed files
    try:
        result = subprocess.run(
            ["git", "diff", "--name-status", f"{base_branch}...{worktree_branch}"],
            cwd=project_path,
            capture_output=True,
            text=True,
            check=True,
        )
        changed_files = []
        for line in result.stdout.strip().split("\n"):
            if line:
                parts = line.split("\t")
                if len(parts) >= 2:
                    status = parts[0]
                    filename = parts[1]
                    changed_files.append(
                        {
                            "path": filename,
                            "status": "added"
                            if status == "A"
                            else "modified"
                            if status == "M"
                            else "deleted"
                            if status == "D"
                            else status,
                        }
                    )
    except subprocess.CalledProcessError:
        changed_files = []

    # Check for potential conflicts using merge-tree (dry run)
    # Git 2.38+ uses new merge-tree format with --write-tree mode by default
    has_conflicts = False
    conflicting_files = []
    try:
        # Use --write-tree explicitly for git 2.38+ behavior
        result = subprocess.run(
            ["git", "merge-tree", "--write-tree", base_branch, worktree_branch],
            cwd=project_path,
            capture_output=True,
            text=True,
        )
        # Git 2.38+: Return code 1 means conflicts exist
        # stdout format: "<tree_oid>\nCONFLICT (type): description"
        if result.returncode == 1:
            has_conflicts = True
            # Parse CONFLICT lines to get conflicting files
            for line in result.stdout.split("\n"):
                if line.startswith("CONFLICT"):
                    # Extract filename from "CONFLICT (content): Merge conflict in path/file"
                    if " in " in line:
                        file_path = line.split(" in ")[-1].strip()
                        if file_path:
                            conflicting_files.append(file_path)
        # Fallback: Check for CONFLICT keyword even on return code 0
        # (some edge cases may not set return code correctly)
        elif "CONFLICT" in result.stdout or "CONFLICT" in result.stderr:
            has_conflicts = True
            for line in (result.stdout + result.stderr).split("\n"):
                if line.startswith("CONFLICT") and " in " in line:
                    file_path = line.split(" in ")[-1].strip()
                    if file_path:
                        conflicting_files.append(file_path)
        # Legacy fallback: Check for conflict markers (older git versions < 2.38)
        elif "<<<<<<" in result.stdout:
            has_conflicts = True
    except subprocess.CalledProcessError as e:
        # Command failed - check output for conflict indicators
        output = (e.stdout or "") + (e.stderr or "")
        if "CONFLICT" in output or "<<<<<<" in output:
            has_conflicts = True
            for line in output.split("\n"):
                if line.startswith("CONFLICT") and " in " in line:
                    file_path = line.split(" in ")[-1].strip()
                    if file_path:
                        conflicting_files.append(file_path)

    # Filter out gitignored files from conflict list (e.g. build artifacts)
    if conflicting_files:
        try:
            result = subprocess.run(
                ["git", "check-ignore"] + conflicting_files,
                cwd=project_path,
                capture_output=True,
                text=True,
            )
            ignored = set(result.stdout.strip().splitlines())
            conflicting_files = [f for f in conflicting_files if f not in ignored]
            if not conflicting_files:
                has_conflicts = False
        except Exception:
            pass  # If check-ignore fails, keep the original list

    # Check if there's an active merge in progress (MERGE_HEAD exists)
    # This is different from the merge-tree dry run above - this means a real merge
    # is in progress with unresolved conflict markers in files
    merge_in_progress = False
    merge_head_file = project_path / ".git" / "MERGE_HEAD"
    if merge_head_file.exists():
        merge_in_progress = True

    # Get commit counts
    try:
        result = subprocess.run(
            ["git", "rev-list", "--count", f"{base_branch}..{worktree_branch}"],
            cwd=project_path,
            capture_output=True,
            text=True,
            check=True,
        )
        commits_ahead = int(result.stdout.strip())
    except (subprocess.CalledProcessError, ValueError):
        commits_ahead = 0

    try:
        result = subprocess.run(
            ["git", "rev-list", "--count", f"{worktree_branch}..{base_branch}"],
            cwd=project_path,
            capture_output=True,
            text=True,
            check=True,
        )
        commits_behind = int(result.stdout.strip())
    except (subprocess.CalledProcessError, ValueError):
        commits_behind = 0

    # Detect uncommitted changes in the main project that could conflict
    uncommitted_files = []
    uncommitted_conflicting_files = []
    try:
        # Get uncommitted files in main project
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=project_path,
            capture_output=True,
            text=True,
            check=True,
        )
        for line in result.stdout.strip().split("\n"):
            if line:
                # Format: "XY filename" or "XY original -> renamed"
                parts = line[3:].split(" -> ")
                filename = parts[-1].strip()  # Use renamed name if present
                if filename:
                    uncommitted_files.append(filename)

        # Get files modified in task branch (for conflict detection)
        if uncommitted_files:
            task_files_result = subprocess.run(
                ["git", "diff", "--name-only", f"{base_branch}...{worktree_branch}"],
                cwd=project_path,
                capture_output=True,
                text=True,
            )
            if task_files_result.returncode == 0:
                task_files = set(task_files_result.stdout.strip().split("\n"))
                # Find files that overlap (uncommitted in main AND modified in task)
                uncommitted_conflicting_files = list(
                    set(uncommitted_files) & task_files
                )

                # Filter out gitignored files (e.g. build artifacts)
                if uncommitted_conflicting_files:
                    try:
                        ignored_result = subprocess.run(
                            ["git", "check-ignore"] + uncommitted_conflicting_files,
                            cwd=project_path,
                            capture_output=True,
                            text=True,
                        )
                        ignored = set(ignored_result.stdout.strip().splitlines())
                        uncommitted_conflicting_files = [
                            f for f in uncommitted_conflicting_files if f not in ignored
                        ]
                    except Exception:
                        pass
    except subprocess.CalledProcessError:
        pass  # Non-fatal - continue without uncommitted detection

    # Run semantic conflict detection using backend merge system
    semantic_conflicts = []
    semantic_stats = {
        "totalFiles": len(changed_files),
        "conflictFiles": 0,
        "totalConflicts": 0,
        "autoMergeable": 0,
        "aiResolved": 0,
        "humanRequired": 0,
    }

    try:
        from ..services.conflict_service import get_conflict_service

        conflict_service = get_conflict_service(project_path)
        semantic_result = await conflict_service.detect_conflicts(
            task_id=task_id,
            worktree_path=worktree_path,
            base_branch=base_branch,
        )

        if semantic_result.get("success"):
            semantic_conflicts = semantic_result.get("conflicts", [])
            semantic_stats = semantic_result.get("stats", semantic_stats)

    except Exception as e:
        # Log but don't fail - semantic detection is optional enhancement
        import logging

        logging.getLogger(__name__).warning(f"Semantic conflict detection failed: {e}")

    # Merge results: combine git conflicts with semantic conflicts
    all_conflicts = semantic_conflicts.copy()

    # Determine overall merge status
    total_conflicts = len(all_conflicts)
    auto_mergeable = sum(1 for c in all_conflicts if c.get("canAutoMerge", False))
    has_any_conflicts = has_conflicts or total_conflicts > 0
    can_merge = not has_conflicts and (
        total_conflicts == 0 or total_conflicts == auto_mergeable
    )

    # Build preview response with all merge information
    preview_data = {
        "files": [f["path"] for f in changed_files],
        "conflicts": all_conflicts,  # Semantic conflicts from merge system
        "summary": {
            "totalFiles": len(changed_files),
            "conflictFiles": semantic_stats.get("conflictFiles", 0),
            "totalConflicts": total_conflicts,
            "autoMergeable": auto_mergeable,
            "aiResolved": semantic_stats.get("aiResolved", 0),
            "humanRequired": total_conflicts - auto_mergeable,
        },
        "gitConflicts": {
            "hasConflicts": has_conflicts,
            "commitsAhead": commits_ahead,
            "commitsBehind": commits_behind,
            "conflictingFiles": conflicting_files,
            "needsRebase": commits_behind > 0,
            "baseBranch": base_branch,
            "specBranch": worktree_branch,
            "mergeInProgress": merge_in_progress,
        },
        "uncommittedChanges": {
            "hasChanges": len(uncommitted_files) > 0,
            "files": uncommitted_files,
            "count": len(uncommitted_files),
            "conflictingFiles": uncommitted_conflicting_files,
            "hasConflicts": len(uncommitted_conflicting_files) > 0,
        }
        if uncommitted_files
        else None,
    }

    return {
        "success": True,
        "data": {
            "canMerge": can_merge,
            "hasConflicts": has_any_conflicts,
            "changedFiles": changed_files,
            "conflicts": all_conflicts,
            "stats": preview_data["summary"],
            "gitConflicts": preview_data["gitConflicts"],
            "worktreeBranch": worktree_branch,
            "baseBranch": base_branch,
            "preview": preview_data,
        },
    }


@router.post("/{task_id}/worktree/resolve-conflicts")
async def resolve_worktree_conflicts(
    task_id: str,
    options: ConflictResolveOptions = None,
    _access: dict = Depends(require_task_access("member")),
):
    """
    Resolve merge conflicts between the worktree branch and the base branch using AI.

    This endpoint performs a real git merge and resolves any conflict markers
    using AI. Process:
    1. Gets the worktree branch name
    2. Starts a git merge of the worktree branch into the current branch
    3. If conflicts arise, uses AI to resolve each conflicted file
    4. Stages resolved files and commits the merge
    """
    import logging

    logger = logging.getLogger(__name__)

    if options is None:
        options = ConflictResolveOptions()

    # Parse task_id to get spec_id
    # task_id could be "project_id:spec_id" or just "spec_id"
    if ":" in task_id:
        project_id, spec_id = task_id.split(":", 1)
        # Look up project path
        projects_file = get_projects_file()
        if not projects_file.exists():
            return {"success": False, "error": "Projects file not found"}

        projects_data = json.loads(projects_file.read_text())

        # Handle dict format where keys are project IDs
        if isinstance(projects_data, dict):
            project = projects_data.get(project_id)
            if not project:
                return {"success": False, "error": f"Project not found: {project_id}"}
            project_path = Path(project["path"])
        else:
            # Handle list format where each item has an "id" field
            project = None
            for p in projects_data:
                if isinstance(p, dict) and p.get("id") == project_id:
                    project = p
                    break
            if not project:
                return {"success": False, "error": f"Project not found: {project_id}"}
            project_path = Path(project["path"])
    else:
        return {
            "success": False,
            "error": "Task ID must include project ID (format: project_id:spec_id)",
        }

    spec_dir = project_path / ".aifactory" / "specs" / spec_id
    worktree_path = project_path / ".aifactory" / "worktrees" / "tasks" / spec_id

    if not spec_dir.exists():
        return {"success": False, "error": f"Task {task_id} not found"}

    if not worktree_path.exists():
        return {"success": False, "error": "No worktree found for this task"}

    # Get worktree branch name
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            check=True,
        )
        worktree_branch = result.stdout.strip()
    except subprocess.CalledProcessError:
        return {"success": False, "error": "Could not determine worktree branch"}

    # Check if a merge is already in progress
    merge_head = project_path / ".git" / "MERGE_HEAD"
    if merge_head.exists():
        logger.info(
            f"Merge already in progress for {task_id}, resolving existing conflicts"
        )
    else:
        # Start the git merge (allow conflicts)
        logger.info(
            f"Starting git merge of {worktree_branch} into current branch for task {task_id}"
        )
        merge_result = subprocess.run(
            ["git", "merge", worktree_branch, "--no-commit", "--no-ff"],
            cwd=project_path,
            capture_output=True,
            text=True,
        )

        if merge_result.returncode == 0:
            # Clean merge, no conflicts - commit it
            logger.info(f"Clean merge for {task_id}, committing")
            commit_result = subprocess.run(
                ["git", "commit", "-m", f"Merge {worktree_branch} into current branch"],
                cwd=project_path,
                capture_output=True,
                text=True,
            )
            return {
                "success": True,
                "data": {
                    "resolved": [],
                    "remaining": [],
                    "stats": {"message": "Clean merge - no conflicts"},
                },
            }
        elif merge_result.returncode != 1 and "CONFLICT" not in merge_result.stdout:
            # Unexpected error (not a conflict)
            logger.error(f"Git merge failed unexpectedly: {merge_result.stderr}")
            return {
                "success": False,
                "error": f"Git merge failed: {merge_result.stderr.strip()}",
            }
        else:
            logger.info(f"Merge has conflicts for {task_id}, resolving with AI")

    if not options.useAI:
        return {
            "success": False,
            "error": "Conflicts detected but AI resolution is disabled",
            "data": {
                "resolved": [],
                "remaining": [],
                "stats": {
                    "message": "Merge started with conflicts, AI resolution disabled"
                },
            },
        }

    # Get list of conflicted files
    conflicted_files = []
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=U"],
            cwd=project_path,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            conflicted_files = [f for f in result.stdout.strip().split("\n") if f]
    except subprocess.CalledProcessError:
        pass

    # Fallback: check git status for unmerged files
    if not conflicted_files:
        try:
            result = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=project_path,
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                for line in result.stdout.strip().split("\n"):
                    if line and line[:2] in ("UU", "AA", "DD", "AU", "UA", "DU", "UD"):
                        file_path = line[3:].strip()
                        if file_path:
                            conflicted_files.append(file_path)
        except subprocess.CalledProcessError:
            pass

    if not conflicted_files:
        # No conflicts found - the merge may have already been resolved
        # Try to commit
        commit_result = subprocess.run(
            ["git", "commit", "--no-edit"],
            cwd=project_path,
            capture_output=True,
            text=True,
        )
        return {
            "success": True,
            "data": {
                "resolved": [],
                "remaining": [],
                "stats": {"message": "No conflicted files found"},
            },
        }

    logger.info(f"Found {len(conflicted_files)} conflicted files: {conflicted_files}")

    # Resolve each conflicted file using AI
    resolved_files = []
    failed_files = []

    from ..services.conflict_service import get_conflict_service

    conflict_service = get_conflict_service(project_path)

    for file_path in conflicted_files:
        try:
            full_path = project_path / file_path
            if not full_path.exists():
                logger.warning(f"Conflicted file not found: {full_path}")
                failed_files.append({"file": file_path, "error": "File not found"})
                continue

            content = full_path.read_text()

            if "<<<<<<< " not in content:
                logger.info(f"File {file_path} has no conflict markers, staging")
                subprocess.run(
                    ["git", "add", file_path],
                    cwd=project_path,
                    capture_output=True,
                    text=True,
                )
                resolved_files.append(file_path)
                continue

            # Use AI to resolve conflict markers
            merge_result = await conflict_service.resolve_conflict_markers(
                file_path=file_path,
                content=content,
            )

            if merge_result.get("success"):
                resolved_content = merge_result.get("content", "")

                # Clean up any remaining markers
                if (
                    "<<<<<<< " in resolved_content
                    or "=======" in resolved_content
                    or ">>>>>>> " in resolved_content
                ):
                    logger.warning(
                        f"AI resolution for {file_path} still has markers, cleaning up"
                    )
                    resolved_content = _clean_conflict_markers(resolved_content)

                full_path.write_text(resolved_content)
                logger.info(f"Wrote resolved content to {full_path}")

                result = subprocess.run(
                    ["git", "add", file_path],
                    cwd=project_path,
                    capture_output=True,
                    text=True,
                )
                if result.returncode == 0:
                    resolved_files.append(file_path)
                    logger.info(f"Staged resolved file: {file_path}")
                else:
                    failed_files.append(
                        {
                            "file": file_path,
                            "error": f"Failed to stage: {result.stderr}",
                        }
                    )
            else:
                error_msg = merge_result.get("error", "AI resolution failed")
                logger.error(f"AI resolution failed for {file_path}: {error_msg}")
                failed_files.append({"file": file_path, "error": error_msg})

        except Exception as e:
            logger.error(f"Failed to resolve {file_path}: {e}")
            failed_files.append({"file": file_path, "error": str(e)})

    if failed_files:
        return {
            "success": len(resolved_files) > 0,
            "data": {
                "resolved": resolved_files,
                "failed": failed_files,
                "remaining": [f["file"] for f in failed_files],
                "stats": {
                    "message": f"Resolved {len(resolved_files)} files, {len(failed_files)} failed"
                },
            },
            "error": f"{len(failed_files)} files could not be resolved",
        }

    # All conflicts resolved - commit the merge
    try:
        commit_msg = f"Merge {worktree_branch} (AI-resolved conflicts)"
        result = subprocess.run(
            ["git", "commit", "-m", commit_msg],
            cwd=project_path,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            logger.warning(f"Merge commit failed: {result.stderr}")
            return {
                "success": True,
                "data": {
                    "resolved": resolved_files,
                    "remaining": [],
                    "stats": {
                        "message": f"Resolved {len(resolved_files)} files but commit failed: {result.stderr.strip()}"
                    },
                },
            }
    except Exception as e:
        logger.warning(f"Merge commit failed: {e}")

    return {
        "success": True,
        "data": {
            "resolved": resolved_files,
            "remaining": [],
            "stats": {
                "message": f"Successfully resolved and merged {len(resolved_files)} conflicting files"
            },
        },
    }


@router.post("/{task_id}/worktree/resolve-uncommitted")
async def resolve_uncommitted_conflicts(
    task_id: str, _access: dict = Depends(require_task_access("member"))
):
    """
    Resolve conflicts between uncommitted local changes and task branch changes using AI.

    This endpoint:
    1. Stashes uncommitted changes in the main project
    2. For each conflicting file, gets the stash, task branch, and base versions
    3. Uses AI to intelligently merge the three versions
    4. Writes merged content to working directory
    5. Drops the stash after successful merge
    """
    import logging

    logger = logging.getLogger(__name__)
    logger.info(f"Resolving uncommitted conflicts for task {task_id}")

    # Find the task's project
    projects_data_dir = get_data_dir()
    projects_file = projects_data_dir / "projects.json"

    if not projects_file.exists():
        return {"success": False, "error": "No projects configured"}

    projects_data = json.loads(projects_file.read_text())

    # Find the task across all projects
    project_path = None
    worktree_path = None

    if isinstance(projects_data, dict):
        projects = list(projects_data.values())
    else:
        projects = projects_data

    for project in projects:
        if isinstance(project, str):
            project_path = Path(project)
        else:
            project_path = Path(project.get("path", ""))

        spec_dir = project_path / ".aifactory" / "specs" / task_id

        if spec_dir.exists():
            worktree_path = (
                project_path / ".aifactory" / "worktrees" / "tasks" / task_id
            )
            break
    else:
        return {"success": False, "error": f"Task {task_id} not found"}

    if not worktree_path or not worktree_path.exists():
        return {"success": False, "error": "No worktree found for this task"}

    # Get branch names
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=project_path,
            capture_output=True,
            text=True,
            check=True,
        )
        base_branch = result.stdout.strip()

        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            check=True,
        )
        spec_branch = result.stdout.strip()
    except subprocess.CalledProcessError:
        return {"success": False, "error": "Could not determine branches"}

    # Get uncommitted files that conflict with task
    uncommitted_files = []
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=project_path,
            capture_output=True,
            text=True,
            check=True,
        )
        for line in result.stdout.strip().split("\n"):
            if line:
                parts = line[3:].split(" -> ")
                filename = parts[-1].strip()
                if filename:
                    uncommitted_files.append(filename)
    except subprocess.CalledProcessError:
        return {"success": False, "error": "Could not get uncommitted files"}

    # Get task branch files
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", f"{base_branch}...{spec_branch}"],
            cwd=project_path,
            capture_output=True,
            text=True,
        )
        task_files = set(result.stdout.strip().split("\n"))
    except subprocess.CalledProcessError:
        task_files = set()

    # Find conflicting files
    conflicting_files = list(set(uncommitted_files) & task_files)

    if not conflicting_files:
        return {
            "success": True,
            "data": {"message": "No conflicting files found", "resolved": []},
        }

    # Stash uncommitted changes (include untracked files)
    stash_message = f"aifactory-temp-{task_id}"
    stash_created = False
    try:
        # First try with --include-untracked to catch new files
        result = subprocess.run(
            ["git", "stash", "push", "--include-untracked", "-m", stash_message],
            cwd=project_path,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and "No local changes to save" not in result.stdout:
            stash_created = True
            logger.info(f"Stashed changes: {result.stdout.strip()}")
        elif result.returncode != 0:
            # Fallback: try without --include-untracked (for older git or if no untracked)
            result = subprocess.run(
                ["git", "stash", "push", "-m", stash_message],
                cwd=project_path,
                capture_output=True,
                text=True,
            )
            if (
                result.returncode == 0
                and "No local changes to save" not in result.stdout
            ):
                stash_created = True
                logger.info(f"Stashed changes (fallback): {result.stdout.strip()}")
            elif result.returncode != 0 and "No local changes to save" not in (
                result.stderr + result.stdout
            ):
                return {
                    "success": False,
                    "error": f"Failed to stash changes: {result.stderr or result.stdout}",
                }
    except subprocess.CalledProcessError as e:
        return {"success": False, "error": f"Failed to stash changes: {e.stderr}"}

    resolved_files = []
    failed_files = []

    try:
        for file_path in conflicting_files:
            try:
                # Get base version (from base branch)
                base_content = ""
                try:
                    result = subprocess.run(
                        ["git", "show", f"{base_branch}:{file_path}"],
                        cwd=project_path,
                        capture_output=True,
                        text=True,
                    )
                    if result.returncode == 0:
                        base_content = result.stdout
                except Exception:
                    pass

                # Get local version (uncommitted changes)
                # If we stashed, get from stash; otherwise read from working directory
                local_content = ""
                try:
                    if stash_created:
                        result = subprocess.run(
                            ["git", "show", f"stash@{{0}}:{file_path}"],
                            cwd=project_path,
                            capture_output=True,
                            text=True,
                        )
                        if result.returncode == 0:
                            local_content = result.stdout
                    else:
                        # Read directly from working directory
                        working_file = project_path / file_path
                        if working_file.exists():
                            local_content = working_file.read_text()
                except Exception:
                    pass

                # Get task branch version
                task_content = ""
                try:
                    result = subprocess.run(
                        ["git", "show", f"{spec_branch}:{file_path}"],
                        cwd=project_path,
                        capture_output=True,
                        text=True,
                    )
                    if result.returncode == 0:
                        task_content = result.stdout
                except Exception:
                    pass

                # Use AI to merge the three versions
                from ..services.conflict_service import get_conflict_service

                conflict_service = get_conflict_service(project_path)
                merge_result = await conflict_service.ai_merge_three_way(
                    file_path=file_path,
                    base_content=base_content,
                    local_content=local_content,
                    task_content=task_content,
                    local_label="your uncommitted changes",
                    task_label=f"task {task_id} changes",
                )

                if merge_result.get("success"):
                    # Write merged content to working directory
                    full_path = project_path / file_path
                    full_path.parent.mkdir(parents=True, exist_ok=True)
                    full_path.write_text(merge_result.get("content", ""))
                    resolved_files.append(file_path)
                else:
                    failed_files.append(
                        {
                            "file": file_path,
                            "error": merge_result.get("error", "Unknown error"),
                        }
                    )

            except Exception as e:
                logger.error(f"Failed to resolve {file_path}: {e}")
                failed_files.append({"file": file_path, "error": str(e)})

    finally:
        # Drop the stash only if we created one
        if stash_created:
            try:
                subprocess.run(
                    ["git", "stash", "drop"],
                    cwd=project_path,
                    capture_output=True,
                    text=True,
                )
                logger.info("Dropped stash after merge")
            except Exception:
                logger.warning("Failed to drop stash - may need manual cleanup")

    if failed_files:
        return {
            "success": len(resolved_files) > 0,
            "data": {
                "resolved": resolved_files,
                "failed": failed_files,
                "message": f"Resolved {len(resolved_files)} files, {len(failed_files)} failed",
            },
            "error": f"{len(failed_files)} files could not be resolved",
        }

    return {
        "success": True,
        "data": {
            "resolved": resolved_files,
            "failed": [],
            "message": f"Successfully resolved {len(resolved_files)} conflicting files",
        },
    }


@router.post("/{task_id}/worktree/resolve-git-merge")
async def resolve_git_merge_conflicts(
    task_id: str, _access: dict = Depends(require_task_access("member"))
):
    """
    Resolve files with git merge conflict markers using AI.

    This endpoint handles the case where a git merge is in progress and files
    contain conflict markers (<<<<<<< HEAD, =======, >>>>>>> branch).

    Unlike resolve_uncommitted_conflicts (which uses stash), this works directly
    with files that already have conflict markers from an in-progress merge.

    Process:
    1. Check if merge is in progress (.git/MERGE_HEAD exists)
    2. Get list of unresolved conflicted files
    3. For each file, use AI to resolve the conflict markers
    4. Write resolved content and stage the file
    5. Return success (user can then commit the merge)
    """
    import logging

    logger = logging.getLogger(__name__)
    logger.info(f"Resolving git merge conflicts for task {task_id}")

    # Find the task's project
    projects_data_dir = get_data_dir()
    projects_file = projects_data_dir / "projects.json"

    if not projects_file.exists():
        return {"success": False, "error": "No projects configured"}

    projects_data = json.loads(projects_file.read_text())

    # Find the task across all projects
    project_path = None
    worktree_path = None

    if isinstance(projects_data, dict):
        projects = list(projects_data.values())
    else:
        projects = projects_data

    for project in projects:
        if isinstance(project, str):
            project_path = Path(project)
        else:
            project_path = Path(project.get("path", ""))

        spec_dir = project_path / ".aifactory" / "specs" / task_id

        if spec_dir.exists():
            worktree_path = (
                project_path / ".aifactory" / "worktrees" / "tasks" / task_id
            )
            break
    else:
        return {"success": False, "error": f"Task {task_id} not found"}

    # Determine which path to work with (main project or worktree)
    # Check both locations for merge in progress
    work_path = None
    merge_head_main = project_path / ".git" / "MERGE_HEAD"
    merge_head_worktree = (
        worktree_path / ".git" if worktree_path and worktree_path.exists() else None
    )

    if merge_head_main.exists():
        work_path = project_path
        logger.info(f"Found merge in progress in main project: {project_path}")
    elif merge_head_worktree and (merge_head_worktree / "MERGE_HEAD").exists():
        work_path = worktree_path
        logger.info(f"Found merge in progress in worktree: {worktree_path}")
    else:
        # No merge in progress - check if there are files with conflict markers anyway
        # This can happen if the merge state was cleared but files still have markers
        logger.info("No MERGE_HEAD found, checking for conflict markers in files...")
        work_path = project_path  # Default to main project

    # Get list of files with unresolved conflicts
    conflicted_files = []
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=U"],
            cwd=work_path,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            conflicted_files = [f for f in result.stdout.strip().split("\n") if f]
            logger.info(
                f"Found {len(conflicted_files)} conflicted files: {conflicted_files}"
            )
    except subprocess.CalledProcessError as e:
        logger.warning(f"git diff --diff-filter=U failed: {e}")

    # If no conflicted files from git, scan for files with conflict markers
    if not conflicted_files:
        logger.info(
            "No files from git diff --diff-filter=U, scanning for conflict markers..."
        )
        try:
            result = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=work_path,
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                for line in result.stdout.strip().split("\n"):
                    if line and (
                        line.startswith("UU")
                        or line.startswith("AA")
                        or line.startswith("DD")
                        or line.startswith("AU")
                        or line.startswith("UA")
                        or line.startswith("DU")
                        or line.startswith("UD")
                    ):
                        # Status codes indicate conflicts
                        file_path = line[3:].strip()
                        if file_path:
                            conflicted_files.append(file_path)
        except subprocess.CalledProcessError:
            pass

    if not conflicted_files:
        return {
            "success": True,
            "data": {
                "resolved": [],
                "failed": [],
                "message": "No conflicted files found",
            },
        }

    # Resolve each conflicted file using AI
    resolved_files = []
    failed_files = []

    from ..services.conflict_service import get_conflict_service

    conflict_service = get_conflict_service(project_path)

    for file_path in conflicted_files:
        try:
            full_path = work_path / file_path
            if not full_path.exists():
                logger.warning(f"Conflicted file not found: {full_path}")
                failed_files.append({"file": file_path, "error": "File not found"})
                continue

            # Read file content with conflict markers
            content = full_path.read_text()

            # Check if file actually has conflict markers
            if "<<<<<<< " not in content:
                logger.info(f"File {file_path} has no conflict markers, skipping")
                # Stage it anyway since git thinks it's conflicted
                subprocess.run(
                    ["git", "add", file_path],
                    cwd=work_path,
                    capture_output=True,
                    text=True,
                )
                resolved_files.append(file_path)
                continue

            # Use AI to resolve conflict markers
            merge_result = await conflict_service.resolve_conflict_markers(
                file_path=file_path,
                content=content,
            )

            if merge_result.get("success"):
                resolved_content = merge_result.get("content", "")

                # Verify no conflict markers remain
                if (
                    "<<<<<<< " in resolved_content
                    or "=======" in resolved_content
                    or ">>>>>>> " in resolved_content
                ):
                    logger.warning(
                        f"AI resolution for {file_path} still contains conflict markers"
                    )
                    # Try to clean up obvious marker remnants
                    resolved_content = _clean_conflict_markers(resolved_content)

                # Write resolved content
                full_path.write_text(resolved_content)
                logger.info(f"Wrote resolved content to {full_path}")

                # Stage the file
                result = subprocess.run(
                    ["git", "add", file_path],
                    cwd=work_path,
                    capture_output=True,
                    text=True,
                )
                if result.returncode == 0:
                    resolved_files.append(file_path)
                    logger.info(f"Staged resolved file: {file_path}")
                else:
                    logger.warning(f"Failed to stage {file_path}: {result.stderr}")
                    failed_files.append(
                        {
                            "file": file_path,
                            "error": f"Failed to stage: {result.stderr}",
                        }
                    )
            else:
                error_msg = merge_result.get("error", "AI resolution failed")
                logger.error(f"AI resolution failed for {file_path}: {error_msg}")
                failed_files.append({"file": file_path, "error": error_msg})

        except Exception as e:
            logger.error(f"Failed to resolve {file_path}: {e}")
            failed_files.append({"file": file_path, "error": str(e)})

    if failed_files:
        return {
            "success": len(resolved_files) > 0,
            "data": {
                "resolved": resolved_files,
                "failed": failed_files,
                "message": f"Resolved {len(resolved_files)} files, {len(failed_files)} failed",
            },
            "error": f"{len(failed_files)} files could not be resolved",
        }

    # All conflicts resolved successfully - auto-commit the merge
    commit_result = None
    try:
        # Get the branch being merged for the commit message
        merge_head_file = work_path / ".git" / "MERGE_HEAD"
        merge_branch = "task branch"
        if merge_head_file.exists():
            merge_commit = merge_head_file.read_text().strip()[:8]
            # Try to get branch name from the merge
            result = subprocess.run(
                ["git", "name-rev", "--name-only", merge_commit],
                cwd=work_path,
                capture_output=True,
                text=True,
            )
            if result.returncode == 0 and result.stdout.strip():
                merge_branch = result.stdout.strip()

        # Commit the merge
        commit_msg = f"Merge {merge_branch} (AI-resolved conflicts)"
        result = subprocess.run(
            ["git", "commit", "-m", commit_msg],
            cwd=work_path,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            commit_result = "Merge committed successfully"
            logger.info(f"Auto-committed merge: {commit_msg}")
        else:
            commit_result = f"Commit failed: {result.stderr}"
            logger.warning(f"Failed to auto-commit merge: {result.stderr}")
    except Exception as e:
        commit_result = f"Commit error: {str(e)}"
        logger.error(f"Error during auto-commit: {e}")

    return {
        "success": True,
        "data": {
            "resolved": resolved_files,
            "failed": [],
            "message": f"Successfully resolved {len(resolved_files)} conflicted files",
            "commit": commit_result,
        },
    }


def _clean_conflict_markers(content: str) -> str:
    """
    Clean up any remaining conflict markers from content.
    This is a fallback if AI resolution leaves some markers.
    """
    import re

    # Pattern to match conflict blocks
    # <<<<<<< ... ======= ... >>>>>>>
    pattern = r"<<<<<<<[^\n]*\n(.*?)=======\n(.*?)>>>>>>>[^\n]*\n?"

    def replace_conflict(match):
        # Prefer the second version (usually "theirs"/incoming changes)
        # This is a simple heuristic - the AI should have already merged properly
        ours = match.group(1)
        theirs = match.group(2)
        # If theirs is empty, use ours
        if not theirs.strip():
            return ours
        return theirs

    cleaned = re.sub(pattern, replace_conflict, content, flags=re.DOTALL)
    return cleaned


@router.post("/{task_id}/worktree/abort-merge")
async def abort_worktree_merge(
    task_id: str, _access: dict = Depends(require_task_access("member"))
):
    """
    Abort a failed merge in the worktree or main project.

    This resets the git state when a merge has left the repository in an
    unmerged/conflicted state. It runs `git merge --abort` in both the
    worktree and the main project to ensure a clean state.
    """
    import logging

    logger = logging.getLogger(__name__)
    logger.info(f"Aborting merge for task {task_id}")

    # Parse task_id to get spec_id
    # task_id could be "project_id:spec_id" or just "spec_id"
    if ":" in task_id:
        project_id, spec_id = task_id.split(":", 1)
        # Look up project path
        projects_file = get_projects_file()
        if not projects_file.exists():
            return {"success": False, "error": "Projects file not found"}

        projects_data = json.loads(projects_file.read_text())

        # Handle dict format where keys are project IDs
        if isinstance(projects_data, dict):
            project = projects_data.get(project_id)
            if not project:
                return {"success": False, "error": f"Project not found: {project_id}"}
            project_path = Path(project["path"])
        else:
            # Handle list format where each item has an "id" field
            project = None
            for p in projects_data:
                if isinstance(p, dict) and p.get("id") == project_id:
                    project = p
                    break
            if not project:
                return {"success": False, "error": f"Project not found: {project_id}"}
            project_path = Path(project["path"])
    else:
        return {
            "success": False,
            "error": "Task ID must include project ID (format: project_id:spec_id)",
        }

    spec_dir = project_path / ".aifactory" / "specs" / spec_id
    if not spec_dir.exists():
        return {"success": False, "error": f"Task {task_id} not found"}

    worktree_path = project_path / ".aifactory" / "worktrees" / "tasks" / spec_id

    aborted_locations = []
    errors = []

    # Try to abort merge in worktree first
    if worktree_path and worktree_path.exists():
        try:
            # Check if worktree is in a merge state
            merge_head = worktree_path / ".git" / "MERGE_HEAD"
            if merge_head.exists() or (worktree_path / "MERGE_HEAD").exists():
                result = subprocess.run(
                    ["git", "merge", "--abort"],
                    cwd=worktree_path,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if result.returncode == 0:
                    aborted_locations.append("worktree")
                    logger.info(f"Aborted merge in worktree: {worktree_path}")
                else:
                    logger.warning(
                        f"Failed to abort merge in worktree: {result.stderr}"
                    )
                    errors.append(f"Worktree: {result.stderr.strip()}")
        except subprocess.TimeoutExpired:
            errors.append("Worktree: git merge --abort timed out")
        except Exception as e:
            logger.error(f"Error aborting merge in worktree: {e}")
            errors.append(f"Worktree: {str(e)}")

    # Try to abort merge in main project
    if project_path and project_path.exists():
        try:
            # Check if main project is in a merge state
            git_dir = project_path / ".git"
            merge_head = (
                git_dir / "MERGE_HEAD"
                if git_dir.is_dir()
                else project_path / ".git" / "MERGE_HEAD"
            )
            if merge_head.exists():
                result = subprocess.run(
                    ["git", "merge", "--abort"],
                    cwd=project_path,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if result.returncode == 0:
                    aborted_locations.append("main project")
                    logger.info(f"Aborted merge in main project: {project_path}")
                else:
                    logger.warning(
                        f"Failed to abort merge in main project: {result.stderr}"
                    )
                    errors.append(f"Main project: {result.stderr.strip()}")
        except subprocess.TimeoutExpired:
            errors.append("Main project: git merge --abort timed out")
        except Exception as e:
            logger.error(f"Error aborting merge in main project: {e}")
            errors.append(f"Main project: {str(e)}")

    if aborted_locations:
        return {
            "success": True,
            "data": {
                "abortedIn": aborted_locations,
                "message": f"Merge aborted in: {', '.join(aborted_locations)}",
            },
        }
    elif errors:
        return {"success": False, "error": "; ".join(errors)}
    else:
        return {
            "success": True,
            "data": {"abortedIn": [], "message": "No active merge found to abort"},
        }


@router.post("/{task_id}/worktree/merge")
async def merge_worktree(
    task_id: str,
    options: WorktreeMergeOptions = None,
    _access: dict = Depends(require_task_access("member")),
):
    """
    Merge the worktree branch into the base branch.
    """
    import subprocess

    if options is None:
        options = WorktreeMergeOptions()

    # Parse task_id to get spec_id
    # task_id could be "project_id:spec_id" or just "spec_id"
    if ":" in task_id:
        project_id, spec_id = task_id.split(":", 1)
        # Look up project path
        projects_file = get_projects_file()
        if not projects_file.exists():
            return {"success": False, "error": "Projects file not found"}

        projects_data = json.loads(projects_file.read_text())

        # Handle dict format where keys are project IDs
        if isinstance(projects_data, dict):
            project = projects_data.get(project_id)
            if not project:
                return {"success": False, "error": f"Project not found: {project_id}"}
            project_path = Path(project["path"])
        else:
            # Handle list format where each item has an "id" field
            project = None
            for p in projects_data:
                if isinstance(p, dict) and p.get("id") == project_id:
                    project = p
                    break
            if not project:
                return {"success": False, "error": f"Project not found: {project_id}"}
            project_path = Path(project["path"])
    else:
        return {
            "success": False,
            "error": "Task ID must include project ID (format: project_id:spec_id)",
        }

    spec_dir = project_path / ".aifactory" / "specs" / spec_id
    if not spec_dir.exists():
        return {"success": False, "error": f"Task {task_id} not found"}

    # Find the worktree
    worktree_path = project_path / ".aifactory" / "worktrees" / "tasks" / spec_id

    if not worktree_path.exists():
        return {"success": False, "error": "No worktree found for this task"}

    # Get the branch name from the worktree
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            check=True,
        )
        worktree_branch = result.stdout.strip()
    except subprocess.CalledProcessError as e:
        return {"success": False, "error": f"Could not determine worktree branch: {e}"}

    # Get the current branch in main repo
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=project_path,
            capture_output=True,
            text=True,
            check=True,
        )
        base_branch = result.stdout.strip()
    except subprocess.CalledProcessError:
        base_branch = "develop"

    # Clean up internal auto-generated files that can block merge
    # These are untracked files created by agents in worktrees that would
    # collide with the same untracked files in the main working directory.
    _INTERNAL_MERGE_BLOCKERS = [
        ".aifactory-security.json",
        ".aifactory-status",
    ]
    for fname in _INTERNAL_MERGE_BLOCKERS:
        blocker = project_path / fname
        if blocker.exists():
            try:
                blocker.unlink()
                logger.info(f"Removed merge-blocking file: {fname}")
            except OSError:
                pass

    # Perform the merge
    try:
        merge_cmd = ["git", "merge", worktree_branch]
        if options.noCommit:
            merge_cmd.append("--no-commit")

        result = subprocess.run(
            merge_cmd, cwd=project_path, capture_output=True, text=True, check=True
        )

        # Clean up worktree after successful merge
        worktree_deleted = False
        branch_deleted = False
        try:
            # Remove git worktree
            cleanup_result = subprocess.run(
                ["git", "worktree", "remove", str(worktree_path), "--force"],
                cwd=project_path,
                capture_output=True,
                text=True,
            )
            worktree_deleted = cleanup_result.returncode == 0

            # Delete the branch (it's merged now)
            branch_result = subprocess.run(
                ["git", "branch", "-d", worktree_branch],
                cwd=project_path,
                capture_output=True,
                text=True,
            )
            branch_deleted = branch_result.returncode == 0
        except Exception as e:
            logger.warning(f"Failed to cleanup worktree after merge: {e}")
            # Don't fail the merge just because cleanup failed

        return {
            "success": True,
            "data": {
                "success": True,  # Frontend checks this for merge result display
                "merged": True,
                "message": f"Successfully merged {worktree_branch} into {base_branch}",
                "output": result.stdout,
                "worktreeDeleted": worktree_deleted,
                "branchDeleted": branch_deleted,
            },
        }
    except subprocess.CalledProcessError as e:
        # Check if it's a conflict
        if "CONFLICT" in e.stdout or "CONFLICT" in e.stderr:
            return {
                "success": False,
                "error": "Merge conflicts detected. Please resolve manually.",
                "conflicts": True,
                "output": e.stdout + e.stderr,
            }
        return {
            "success": False,
            "error": f"Merge failed: {e.stderr or e.stdout}",
            "output": e.stdout + e.stderr,
        }


@router.get("/{task_id}/worktree/status")
async def get_worktree_status(
    task_id: str, _access: dict = Depends(require_task_access("viewer"))
):
    """
    Get the status of a task's worktree.
    Returns information about the worktree including changed files count,
    additions/deletions, and whether it exists.
    """
    import subprocess

    # Parse task_id to get project_id and spec_id
    if ":" in task_id:
        project_id, spec_id = task_id.split(":", 1)
    else:
        # task_id is just the spec_id, search for project
        spec_id = task_id
        project_id = None

    # Find project path
    projects_data_dir = get_data_dir()
    projects_file = projects_data_dir / "projects.json"

    if not projects_file.exists():
        return {
            "success": True,
            "data": {
                "exists": False,
            },
        }

    projects_data = json.loads(projects_file.read_text())

    # Handle both dict format (id -> project) and list format
    project_path = None
    if isinstance(projects_data, dict):
        if project_id and project_id in projects_data:
            project_path = Path(projects_data[project_id]["path"])
        else:
            # Search all projects for this spec
            for proj in projects_data.values():
                path = Path(proj["path"])
                if (path / ".aifactory" / "specs" / spec_id).exists():
                    project_path = path
                    break
    else:
        for project in projects_data:
            path = Path(project.get("path", ""))
            if project_id and project.get("id") == project_id:
                project_path = path
                break
            elif (path / ".aifactory" / "specs" / spec_id).exists():
                project_path = path
                break

    if not project_path:
        return {
            "success": True,
            "data": {
                "exists": False,
            },
        }

    # Check for worktree
    worktree_path = project_path / ".aifactory" / "worktrees" / "tasks" / spec_id

    if not worktree_path.exists():
        return {
            "success": True,
            "data": {
                "exists": False,
            },
        }

    # Get worktree branch
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            check=True,
        )
        worktree_branch = result.stdout.strip()
    except subprocess.CalledProcessError:
        worktree_branch = f"aifactory/{spec_id}"

    # Get base branch from main project
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=project_path,
            capture_output=True,
            text=True,
            check=True,
        )
        base_branch = result.stdout.strip()
    except subprocess.CalledProcessError:
        base_branch = "develop"

    # Count commits ahead
    try:
        result = subprocess.run(
            ["git", "rev-list", "--count", f"{base_branch}..{worktree_branch}"],
            cwd=project_path,
            capture_output=True,
            text=True,
            check=True,
        )
        commit_count = int(result.stdout.strip())
    except (subprocess.CalledProcessError, ValueError):
        commit_count = 0

    # Get changed files stats
    files_changed = 0
    additions = 0
    deletions = 0

    try:
        result = subprocess.run(
            ["git", "diff", "--stat", f"{base_branch}...{worktree_branch}"],
            cwd=project_path,
            capture_output=True,
            text=True,
            check=True,
        )
        # Parse the last line for summary (e.g., "5 files changed, 100 insertions(+), 20 deletions(-)")
        lines = result.stdout.strip().split("\n")
        if lines:
            summary_line = lines[-1]
            import re

            files_match = re.search(r"(\d+) files? changed", summary_line)
            if files_match:
                files_changed = int(files_match.group(1))
            insert_match = re.search(r"(\d+) insertions?\(\+\)", summary_line)
            if insert_match:
                additions = int(insert_match.group(1))
            del_match = re.search(r"(\d+) deletions?\(-\)", summary_line)
            if del_match:
                deletions = int(del_match.group(1))
    except subprocess.CalledProcessError:
        pass

    return {
        "success": True,
        "data": {
            "exists": True,
            "worktreePath": str(worktree_path),
            "branch": worktree_branch,
            "baseBranch": base_branch,
            "commitCount": commit_count,
            "filesChanged": files_changed,
            "additions": additions,
            "deletions": deletions,
        },
    }


@router.get("/{task_id}/worktree/diff")
async def get_worktree_diff(
    task_id: str, _access: dict = Depends(require_task_access("viewer"))
):
    """
    Get the diff details for a task's worktree.
    Returns detailed file-by-file changes between the worktree branch and base branch.
    """
    import subprocess

    # Parse task_id to get project_id and spec_id
    if ":" in task_id:
        project_id, spec_id = task_id.split(":", 1)
    else:
        spec_id = task_id
        project_id = None

    # Find project path
    projects_data_dir = get_data_dir()
    projects_file = projects_data_dir / "projects.json"

    if not projects_file.exists():
        return {"success": False, "error": "No projects configured"}

    projects_data = json.loads(projects_file.read_text())

    # Handle both dict format (id -> project) and list format
    project_path = None
    if isinstance(projects_data, dict):
        if project_id and project_id in projects_data:
            project_path = Path(projects_data[project_id]["path"])
        else:
            for proj in projects_data.values():
                path = Path(proj["path"])
                if (path / ".aifactory" / "specs" / spec_id).exists():
                    project_path = path
                    break
    else:
        for project in projects_data:
            path = Path(project.get("path", ""))
            if project_id and project.get("id") == project_id:
                project_path = path
                break
            elif (path / ".aifactory" / "specs" / spec_id).exists():
                project_path = path
                break

    if not project_path:
        return {"success": False, "error": f"Project not found for task {task_id}"}

    # Check for worktree
    worktree_path = project_path / ".aifactory" / "worktrees" / "tasks" / spec_id

    if not worktree_path.exists():
        return {"success": False, "error": "No worktree found for this task"}

    # Get worktree branch
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            check=True,
        )
        worktree_branch = result.stdout.strip()
    except subprocess.CalledProcessError:
        worktree_branch = f"aifactory/{spec_id}"

    # Get base branch from main project
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=project_path,
            capture_output=True,
            text=True,
            check=True,
        )
        base_branch = result.stdout.strip()
    except subprocess.CalledProcessError:
        base_branch = "develop"

    # Get detailed diff with numstat
    files = []
    try:
        result = subprocess.run(
            ["git", "diff", "--numstat", f"{base_branch}...{worktree_branch}"],
            cwd=project_path,
            capture_output=True,
            text=True,
            check=True,
        )
        for line in result.stdout.strip().split("\n"):
            if line:
                parts = line.split("\t")
                if len(parts) >= 3:
                    added = parts[0]
                    deleted = parts[1]
                    path = parts[2]
                    # Handle binary files (show as -)
                    additions = int(added) if added != "-" else 0
                    deletions = int(deleted) if deleted != "-" else 0
                    files.append(
                        {
                            "path": path,
                            "status": "modified",  # Will be refined below
                            "additions": additions,
                            "deletions": deletions,
                        }
                    )
    except subprocess.CalledProcessError:
        pass

    # Get file statuses (A/M/D/R)
    try:
        result = subprocess.run(
            ["git", "diff", "--name-status", f"{base_branch}...{worktree_branch}"],
            cwd=project_path,
            capture_output=True,
            text=True,
            check=True,
        )
        status_map = {}
        for line in result.stdout.strip().split("\n"):
            if line:
                parts = line.split("\t")
                if len(parts) >= 2:
                    status_code = parts[0][0]  # First char (R100 -> R)
                    filename = parts[-1]  # Last part is the filename
                    status = "modified"
                    if status_code == "A":
                        status = "added"
                    elif status_code == "D":
                        status = "deleted"
                    elif status_code == "R":
                        status = "renamed"
                    elif status_code == "M":
                        status = "modified"
                    status_map[filename] = status

        # Update files with proper status
        for f in files:
            if f["path"] in status_map:
                f["status"] = status_map[f["path"]]
    except subprocess.CalledProcessError:
        pass

    # Filter out internal aifactory files and agent artifacts (not relevant for user review)
    INTERNAL_FILES = {".aifactory-security.json", ".aifactory-status"}
    INTERNAL_PREFIXES = (".aifactory/", "VERIFICATION_REPORT", "LANGUAGE_CHOICE")
    files = [
        f
        for f in files
        if f["path"] not in INTERNAL_FILES
        and not any(f["path"].startswith(p) for p in INTERNAL_PREFIXES)
    ]

    # Fallback: if git diff shows no user-facing files but worktree has changes,
    # list files that exist in worktree but not in the main project
    if not files and worktree_path.exists():
        for f in worktree_path.iterdir():
            # Skip internal files, directories, and dotfiles
            if f.name.startswith(".") or f.name.startswith("__") or f.is_dir():
                continue
            if f.name in INTERNAL_FILES or any(
                f.name.startswith(p) for p in INTERNAL_PREFIXES
            ):
                continue
            # Check if this file exists in the main project
            main_file = project_path / f.name
            if not main_file.exists():
                # New file created by the agent
                try:
                    content = f.read_text(errors="replace")
                    line_count = content.count("\n") + (
                        1 if content and not content.endswith("\n") else 0
                    )
                    # Generate a unified diff for display
                    diff_lines = ["--- /dev/null", f"+++ b/{f.name}"]
                    diff_lines.append(f"@@ -0,0 +1,{line_count} @@")
                    for line in content.splitlines():
                        diff_lines.append(f"+{line}")
                    synthetic_diff = "\n".join(diff_lines) + "\n"
                except OSError:
                    line_count = 0
                    synthetic_diff = ""
                files.append(
                    {
                        "path": f.name,
                        "status": "added",
                        "additions": line_count,
                        "deletions": 0,
                        "diff": synthetic_diff,
                    }
                )

    # Get actual diff content for each file
    for f in files:
        try:
            result = subprocess.run(
                ["git", "diff", f"{base_branch}...{worktree_branch}", "--", f["path"]],
                cwd=project_path,
                capture_output=True,
                text=True,
                check=True,
            )
            f["diff"] = result.stdout
        except subprocess.CalledProcessError:
            # If diff fails for a file, leave diff empty
            f["diff"] = ""

    # Generate summary
    total_additions = sum(f["additions"] for f in files)
    total_deletions = sum(f["deletions"] for f in files)
    summary = f"{len(files)} files changed, +{total_additions} -{total_deletions}"

    return {
        "success": True,
        "data": {
            "files": files,
            "summary": summary,
        },
    }


@router.post("/{task_id}/worktree/discard")
async def discard_worktree(
    task_id: str, _access: dict = Depends(require_task_access("admin"))
):
    """
    Discard/delete the worktree for a task.
    Removes the worktree directory and optionally the branch.
    """
    # Parse task_id to get spec_id
    # task_id could be "project_id:spec_id" or just "spec_id"
    if ":" in task_id:
        project_id, spec_id = task_id.split(":", 1)
        # Look up project path
        projects_file = get_projects_file()
        if not projects_file.exists():
            return {"success": False, "error": "Projects file not found"}

        import json

        projects_data = json.loads(projects_file.read_text())

        # Handle dict format where keys are project IDs
        if isinstance(projects_data, dict):
            project = projects_data.get(project_id)
            if not project:
                return {"success": False, "error": f"Project not found: {project_id}"}
            project_path = Path(project["path"])
        else:
            # Handle list format where each item has an "id" field
            project = None
            for p in projects_data:
                if isinstance(p, dict) and p.get("id") == project_id:
                    project = p
                    break
            if not project:
                return {"success": False, "error": f"Project not found: {project_id}"}
            project_path = Path(project["path"])
    else:
        # task_id is just the spec_id, need to find project from context
        return {
            "success": False,
            "error": "Task ID must include project ID (format: project_id:spec_id)",
        }

    # Find the worktree
    worktree_path = project_path / ".aifactory" / "worktrees" / "tasks" / spec_id

    if not worktree_path.exists():
        return {"success": False, "error": "No worktree found for this task"}

    try:
        # Get the branch name before removing worktree
        branch_name = f"aifactory/{spec_id}"

        # Remove worktree using git command
        result = subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree_path)],
            cwd=project_path,
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            # Fallback: force delete directory
            if worktree_path.exists():
                shutil.rmtree(worktree_path, ignore_errors=True)

        # Prune worktrees
        subprocess.run(
            ["git", "worktree", "prune"],
            cwd=project_path,
            capture_output=True,
            text=True,
        )

        # Delete the branch
        subprocess.run(
            ["git", "branch", "-D", branch_name],
            cwd=project_path,
            capture_output=True,
            text=True,
        )

        return {
            "success": True,
            "data": {
                "discarded": True,
                "message": f"Successfully discarded worktree for {spec_id}",
            },
        }
    except Exception as e:
        return {"success": False, "error": f"Failed to discard worktree: {str(e)}"}


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
