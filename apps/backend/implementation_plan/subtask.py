#!/usr/bin/env python3
"""
Subtask Models
==============

Defines a single unit of implementation work with tracking, verification,
and output capabilities.
"""

from dataclasses import dataclass, field
from datetime import datetime

from .enums import SubtaskStatus
from .verification import Verification

# Subtask.service values that belong to a downstream PARR stage (TFactory tests,
# CI pipeline) rather than the AIFactory coding agent. See Subtask.is_handoff.
_HANDOFF_SERVICES = frozenset({"testing", "cicd"})


@dataclass
class Subtask:
    """A single unit of implementation work."""

    id: str
    description: str
    status: SubtaskStatus = SubtaskStatus.PENDING

    # Scoping
    service: str | None = None  # Which service (backend, frontend, worker)
    all_services: bool = False  # True for integration subtasks

    # Dependencies (#376: enables dependency-graph wave scheduling)
    # IDs of subtasks that must complete before this one can start. Empty means
    # the subtask has no prerequisites and may run in the first wave.
    depends_on: list[str] = field(default_factory=list)

    # Optional per-subtask model override (#376 right-sizing). When set, this
    # subtask runs on the given model/shorthand (e.g. "haiku" for mechanical
    # scaffolding) instead of the phase default — cheaper/faster where the work
    # is simple, while planner/QA stay on the stronger model. None = phase model.
    model: str | None = None

    # Files
    files_to_modify: list[str] = field(default_factory=list)
    files_to_create: list[str] = field(default_factory=list)
    patterns_from: list[str] = field(default_factory=list)

    # Verification
    verification: Verification | None = None

    # What this subtask promised. PFactory's children carry them and the planner
    # emits them, but the model did not, so every load/save round-trip DELETED
    # them from implementation_plan.json — and the honesty gates read them: the
    # CI/CD gate (#1113) learns which pipeline stages a subtask demands from
    # here, and the wave engine judges a subtask through ``to_dict()`` (#1177),
    # so an unmodelled field is a gate that silently sees nothing.
    acceptance_criteria: list[str] = field(default_factory=list)

    # For investigation subtasks
    expected_output: str | None = None  # Knowledge/decision output
    actual_output: str | None = None  # What was discovered

    # Tracking
    started_at: str | None = None
    completed_at: str | None = None
    session_id: int | None = None  # Which session completed this

    # Self-Critique
    critique_result: dict | None = None  # Results from self-critique before completion

    @property
    def is_handoff(self) -> bool:
        """True when this subtask belongs to a downstream PARR stage, not the
        AIFactory coder.

        PFactory's decomposition can emit ``testing`` / ``cicd`` children (it
        tags them via ``service``). Those are TFactory's / the CI pipeline's job,
        not work the coding agent should attempt — left in the coder's queue they
        get a "SESSION 2" that fails (no committable output), which falsely marks
        the whole coding phase ``failed`` even though the implementation work is
        complete. The coder skips these and they don't block coding completion;
        the full contract still carries them to TFactory via the handoff.
        """
        return (self.service or "").lower() in _HANDOFF_SERVICES

    def to_dict(self) -> dict:
        """Convert to dictionary representation."""
        result = {
            "id": self.id,
            "description": self.description,
            "status": self.status.value,
        }
        if self.service:
            result["service"] = self.service
        if self.all_services:
            result["all_services"] = True
        if self.depends_on:
            result["depends_on"] = self.depends_on
        if self.model:
            result["model"] = self.model
        if self.files_to_modify:
            result["files_to_modify"] = self.files_to_modify
        if self.files_to_create:
            result["files_to_create"] = self.files_to_create
        if self.patterns_from:
            result["patterns_from"] = self.patterns_from
        if self.verification:
            result["verification"] = self.verification.to_dict()
        if self.acceptance_criteria:
            result["acceptance_criteria"] = self.acceptance_criteria
        if self.expected_output:
            result["expected_output"] = self.expected_output
        if self.actual_output:
            result["actual_output"] = self.actual_output
        if self.started_at:
            result["started_at"] = self.started_at
        if self.completed_at:
            result["completed_at"] = self.completed_at
        if self.session_id is not None:
            result["session_id"] = self.session_id
        if self.critique_result:
            result["critique_result"] = self.critique_result
        return result

    @classmethod
    def from_dict(cls, data: dict) -> "Subtask":
        """Create Subtask from dictionary."""
        verification = None
        if "verification" in data:
            verification = Verification.from_dict(data["verification"])

        return cls(
            id=data["id"],
            description=data["description"],
            status=SubtaskStatus(data.get("status", "pending")),
            service=data.get("service"),
            all_services=data.get("all_services", False),
            depends_on=data.get("depends_on", []),
            model=data.get("model"),
            files_to_modify=data.get("files_to_modify", []),
            files_to_create=data.get("files_to_create", []),
            patterns_from=data.get("patterns_from", []),
            verification=verification,
            acceptance_criteria=data.get("acceptance_criteria") or [],
            expected_output=data.get("expected_output"),
            actual_output=data.get("actual_output"),
            started_at=data.get("started_at"),
            completed_at=data.get("completed_at"),
            session_id=data.get("session_id"),
            critique_result=data.get("critique_result"),
        )

    def start(self, session_id: int):
        """Mark subtask as in progress."""
        self.status = SubtaskStatus.IN_PROGRESS
        self.started_at = datetime.now().isoformat()
        self.session_id = session_id
        # Clear stale data from previous runs to ensure clean state
        self.completed_at = None
        self.actual_output = None

    def complete(self, output: str | None = None):
        """Mark subtask as done."""
        self.status = SubtaskStatus.COMPLETED
        self.completed_at = datetime.now().isoformat()
        if output:
            self.actual_output = output

    def fail(self, reason: str | None = None):
        """Mark subtask as failed."""
        self.status = SubtaskStatus.FAILED
        self.completed_at = None  # Clear to maintain consistency (failed != completed)
        if reason:
            self.actual_output = f"FAILED: {reason}"


# Backwards compatibility alias
Chunk = Subtask
