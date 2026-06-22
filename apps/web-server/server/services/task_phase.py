"""Task phase model + phase/progress helpers — from services/agent_service.py.

Pure module-level task-phase enum, status/review-reason mappings, build-failure
detection and progress scaling, lifted out of the agent_service god-file so they
have a clear home and can be imported without importing AgentService.
services/agent_service.py re-exports every name, so existing callers are
unchanged. This module imports nothing from agent_service -> no circular import.
"""

from __future__ import annotations

from enum import Enum


class TaskPhase(str, Enum):
    """Task execution phases."""

    SPEC_CREATION = "spec_creation"
    PLANNING = "planning"
    PLAN_REVIEW = "plan_review"  # Paused for human plan approval
    CODING = "coding"
    QA_REVIEW = "qa_review"
    QA_FIXING = "qa_fixing"
    COMPLETED = "completed"
    FAILED = "failed"


def _append_parallel_flags(
    cmd: list[str], parallel: bool | None, workers: int | None
) -> bool:
    """Append run.py parallel flags (#376) to ``cmd`` in place.

    Returns True when ``--parallel`` was added (so the caller can log it).
    Extracted as a pure helper so the route→executor flag threading is unit
    testable without spawning a subprocess.
    """
    if not parallel:
        return False
    cmd.append("--parallel")
    if workers and workers > 0:
        cmd.extend(["--workers", str(workers)])
    return True


def phase_to_status(phase: TaskPhase) -> str:
    """Map execution phase to task status for kanban column placement."""
    mapping = {
        TaskPhase.SPEC_CREATION: "in_progress",
        TaskPhase.PLANNING: "in_progress",
        TaskPhase.PLAN_REVIEW: "human_review",  # Paused for human plan approval
        TaskPhase.CODING: "in_progress",
        TaskPhase.QA_REVIEW: "ai_review",
        TaskPhase.QA_FIXING: "in_progress",
        TaskPhase.COMPLETED: "human_review",
        TaskPhase.FAILED: "human_review",
    }
    return mapping.get(phase, "in_progress")


def phase_to_review_reason(phase: TaskPhase) -> str | None:
    """Map execution phase to reviewReason field value.

    Returns the appropriate reviewReason for phases that result in human_review status:
    - PLAN_REVIEW: "plan_review" (waiting for plan approval before coding)
    - COMPLETED: "completed" (task finished successfully, needs final approval)
    - FAILED: "errors" (task failed, needs human intervention)

    Returns None for phases that don't require a reviewReason.
    """
    mapping = {
        TaskPhase.PLAN_REVIEW: "plan_review",
        TaskPhase.COMPLETED: "completed",
        TaskPhase.FAILED: "errors",
    }
    return mapping.get(phase)


# Subtask statuses that count as "did not succeed" when deciding whether a
# build that exited cleanly actually produced anything (Issue #287).
_FAILED_SUBTASK_STATUSES = frozenset({"failed", "stuck", "error", "blocked"})


def is_failed_build(plan: dict) -> bool:
    """Return True when a finished build did NOT actually succeed.

    Issue #287: a build whose process exits 0 but where NO subtask completed
    and at least one subtask failed/stuck still got mapped to the COMPLETED
    phase → ``human_review`` + reviewReason ``"completed"``, masking total
    failure as review-ready success (empty diff, "0 done / N failed").

    Conservative by design — only flips to failure when there was genuinely
    no progress:

    - At least one subtask exists (an empty/invalid plan is handled elsewhere).
    - ZERO subtasks reached ``completed``.
    - At least one subtask is in a failed/stuck state.

    A build with SOME completed subtasks (even alongside failures) is a real
    partial-review case and returns False, preserving the genuine human-review
    path. An all-pending plan (e.g. nothing ran) also returns False so we don't
    mislabel other flows.
    """
    completed = 0
    failed = 0
    total = 0
    for phase in plan.get("phases", []):
        for subtask in phase.get("subtasks", []):
            total += 1
            status = subtask.get("status", "pending")
            if status == "completed":
                completed += 1
            elif status in _FAILED_SUBTASK_STATUSES:
                failed += 1

    return total > 0 and completed == 0 and failed >= 1


# Phase ranges for overall progress scaling (start%, end%)
# Maps within-phase progress (0-100) to an overall range so progress is monotonically increasing.
PHASE_RANGES: dict[str, tuple[float, float]] = {
    "spec_creation": (0, 20),
    "planning": (0, 20),
    "plan_review": (20, 20),  # Fixed at 20%
    "coding": (20, 80),
    "qa_review": (80, 95),
    "qa_fixing": (80, 95),
    "completed": (95, 100),
    "failed": (0, 0),  # Keep whatever was last
}


def scale_progress(phase: str, phase_progress: float) -> float:
    """Scale within-phase progress (0-100) to overall progress range.

    Example: coding phase at 50% → 20 + (50/100) × 60 = 50% overall.
    """
    start, end = PHASE_RANGES.get(phase, (0, 100))
    width = end - start
    return round(start + (phase_progress / 100) * width)
