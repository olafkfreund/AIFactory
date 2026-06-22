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
    github_issue: int | None = Field(
        None,
        description="Upstream GitHub issue number (RFC-0001 correlation key) from "
        "PFactory provenance; lets the cockpit thread plan→code→test.",
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


# Keys preferred (in order) when collapsing a stringified mapping down to a
# single readable line for the cockpit Overview.
_DESC_PREFERRED_KEYS = (
    "description",
    "summary",
    "brief",
    "text",
    "title",
    "epic_title",
    "plan_id",
)

# Matches an embedded ``Correlation epic #{...}`` run where a whole dict was
# str()-ed into otherwise-clean prose. Non-greedy + DOTALL so it collapses a
# multi-line dict body while leaving the surrounding prose intact.
_CORRELATION_EPIC_DICT_RE = re.compile(r"Correlation epic #\{.*?\}", re.DOTALL)

# Best-effort extraction of a plan_id out of such an embedded dict run.
_PLAN_ID_RE = re.compile(r"['\"]plan_id['\"]\s*:\s*['\"]([^'\"]+)['\"]")

# Max length of the single-line fallback when a stringified mapping cannot be
# parsed, so the cockpit never receives a wall of (collapsed) dict text.
_FALLBACK_MAX_LEN = 200


def _collapse_correlation_epic(desc: str) -> str:
    """Collapse an embedded ``Correlation epic #{...dict...}`` run.

    Replaces the ``#{...}`` blob with ``#<plan_id>`` when a plan_id can be
    found, otherwise with a bare ``Correlation epic`` reference. Prose around
    the blob is preserved.
    """

    def _replace(match: re.Match[str]) -> str:
        plan_match = _PLAN_ID_RE.search(match.group(0))
        if plan_match:
            return f"Correlation epic #{plan_match.group(1)}"
        return "Correlation epic"

    return _CORRELATION_EPIC_DICT_RE.sub(_replace, desc)


def _looks_like_stringified_mapping(stripped: str) -> bool:
    """Conservatively detect a value that is a whole dict str()-ed to text.

    Requires the value to both start with ``{`` and end with ``}`` and contain
    a mapping-style ``key:`` separator (``':`` for Python reprs / single-quoted
    JSON, or ``": `` for JSON). Prose with a stray brace is left untouched.
    """
    return (
        stripped.startswith("{")
        and stripped.endswith("}")
        and ("':" in stripped or '": ' in stripped)
    )


def _summarize_mapping(mapping: dict) -> str:
    """Pick a short readable string out of a parsed mapping.

    Prefers a human-readable field (description/summary/brief/text), then a
    title-like field, then plan_id. Only ever returns a scalar string value —
    never anything dict/list-shaped — so the cockpit cannot render dict guts.
    """
    for key in _DESC_PREFERRED_KEYS:
        value = mapping.get(key)
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            text = str(value).strip()
            if text:
                return text
    return ""


def _clean_task_description(desc: str) -> str:
    """Defensive guard so the cockpit Overview never renders dict guts.

    The common case (normal markdown / prose) is returned unchanged. This is a
    belt-and-suspenders product guard against upstream data that str()-ed a
    whole plan/epic dict into the task description (see aifactory-demo#206,
    AIFactory#612). It never raises.
    """
    if not isinstance(desc, str):
        return desc

    stripped = desc.strip()
    if not stripped:
        return desc

    # Case 1: the entire description IS a stringified dict/JSON mapping.
    if _looks_like_stringified_mapping(stripped):
        parsed: object = None
        for loader in (json.loads, ast.literal_eval):
            try:
                parsed = loader(stripped)
            except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
                continue
            else:
                break

        if isinstance(parsed, dict):
            summary = _summarize_mapping(parsed)
            if summary:
                return summary

        # Parsing failed (or yielded nothing usable): collapse to a single
        # line, strip the outer braces, and truncate so no multi-line dict
        # guts ever reach the renderer.
        flattened = " ".join(stripped.strip("{}").split())
        if len(flattened) > _FALLBACK_MAX_LEN:
            flattened = flattened[:_FALLBACK_MAX_LEN].rstrip() + "…"
        return flattened

    # Case 2: clean prose that merely embeds a ``Correlation epic #{...}`` run.
    if "Correlation epic #{" in desc:
        return _collapse_correlation_epic(desc)

    return desc


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
        "github_issue": None,
    }

    # Try to load requirements.json for title/description (most accurate source)
    requirements_file = spec_dir / "requirements.json"
    if requirements_file.exists():
        try:
            requirements = json.loads(requirements_file.read_text())
            if "title" in requirements:
                metadata["title"] = requirements["title"]
            if "description" in requirements:
                metadata["description"] = _clean_task_description(
                    requirements["description"]
                )
            # RFC-0001 correlation: surface the upstream GitHub issue so the
            # cockpit threads this task with its PFactory plan + TFactory test.
            prov = requirements.get("provenance")
            if isinstance(prov, dict) and prov.get("issue_number") is not None:
                metadata["github_issue"] = prov.get("issue_number")
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
                    metadata["description"] = _clean_task_description(p.strip())
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
        github_issue=metadata.get("github_issue"),
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
        # RFC-0001 correlation: surface the upstream GitHub issue so the cockpit
        # threads this build with its PFactory plan + TFactory test.
        "githubIssueNumber": task.github_issue,
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
