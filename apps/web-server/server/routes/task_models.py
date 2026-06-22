"""Core task API models — extracted from routes/tasks.py (#556 god-file split).

These Pydantic models (and the TaskStatus literal) live in their own module so
sub-routers can import them WITHOUT importing routes/tasks.py — which would be a
circular import, since tasks.py mounts those sub-routers at its tail. routes/
tasks.py re-exports every name from here, so existing
``from ..routes.tasks import Task`` callers are unchanged.
"""

from __future__ import annotations

from pathlib import Path  # noqa: F401  (referenced by model field annotations)
from typing import Literal, Optional  # noqa: F401

from pydantic import BaseModel, Field

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
    status: Literal["pending", "in_progress", "completed", "failed", "blocked"] = (
        "pending"
    )
    files: list[str] = Field(default_factory=list)  # Files affected by this subtask
    verification: SubtaskVerification | None = None  # How to verify completion
    # Dependency-graph + timing fields (#94: feeds the cockpit's live execution
    # diagram). Additive + optional — older plans without them serialize as
    # [] / null and the diagram degrades gracefully.
    depends_on: list[str] = Field(default_factory=list)  # IDs of prerequisite subtasks
    service: str | None = (
        None  # Which service (backend/frontend/worker) — diagram accent
    )
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
