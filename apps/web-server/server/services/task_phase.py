"""Task phase model + phase/progress helpers — from services/agent_service.py.

Pure module-level task-phase enum, status/review-reason mappings, build-failure
detection and progress scaling, lifted out of the agent_service god-file so they
have a clear home and can be imported without importing AgentService.
services/agent_service.py re-exports every name, so existing callers are
unchanged. This module imports nothing from agent_service -> no circular import.
"""

from __future__ import annotations

import json
import logging
from enum import Enum
from pathlib import Path

from factory_common.logsafe import sanitize_log

_log = logging.getLogger(__name__)


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


def _append_quick_mode_flag(cmd: list[str], mode: str | None) -> bool:
    """Append run.py's ``--skip-qa`` for a quick-mode build (#916) in place.

    Quick mode's coder prompt validates inline, so the separate QA loop is
    skipped. One rule for BOTH build paths — the in-pod subprocess
    (``agent_service._spawn_task_execution``) and the k8s Job manifest
    (``build_backend.build_run_py_job_manifest``) — because a second copy of a
    per-flag rule is exactly how ``--parallel`` (#914) and ``--force`` (#916)
    already went missing from the Job argv.

    Returns True when the flag was added (so the caller can log it).
    """
    if mode != "quick":
        return False
    cmd.append("--skip-qa")
    return True


def sync_require_review_metadata(spec_dir: Path) -> bool:
    """Sync ``requireReviewBeforeCoding`` frontend->backend and return it.

    The frontend writes the flag into ``requirements.json``; the backend (the
    coder's gate) reads ``task_metadata.json``. Keep them in sync, then report
    the effective value. Lifted from ``agent_service._spawn_task_execution`` so
    the kubejob dispatch path reads the flag the SAME way instead of ignoring
    it (#916).

    One deliberate narrowing vs the original: task_metadata.json is written only
    when there is a value to sync, where the original rewrote it on every build
    with a requirements.json (creating an empty ``{}`` for specs that had none).
    Same answer either way — every reader treats a missing file and an empty one
    identically — and this path now also runs while building a Job manifest, so
    it should not litter the spec dir as a side effect of being asked a question.
    """
    requirements_file = spec_dir / "requirements.json"
    task_metadata_file = spec_dir / "task_metadata.json"

    if requirements_file.exists():
        try:
            requirements = json.loads(requirements_file.read_text())
            frontend_metadata = requirements.get("metadata", {})
            task_metadata = (
                json.loads(task_metadata_file.read_text())
                if task_metadata_file.exists()
                else {}
            )
            if "requireReviewBeforeCoding" in frontend_metadata:
                task_metadata["requireReviewBeforeCoding"] = frontend_metadata[
                    "requireReviewBeforeCoding"
                ]
                task_metadata_file.write_text(json.dumps(task_metadata, indent=2))
            return bool(task_metadata.get("requireReviewBeforeCoding", False))
        except (json.JSONDecodeError, OSError) as e:
            # FAIL CLOSED. This helper's return value IS the decision, so
            # returning False here tells the caller "no review requested" when
            # the truth is "I could not find out". Only one of those is safe
            # (AIFactory#1384, TFactory#1139).
            _log.warning(
                "[AgentService] Could not sync metadata for %s (%s); requiring "
                "human review rather than reporting none was requested",
                sanitize_log(spec_dir),
                sanitize_log(e),
            )
            return True

    if task_metadata_file.exists():
        try:
            task_metadata = json.loads(task_metadata_file.read_text())
            # Quick Mode does NOT force review here — respect the explicit
            # requireReviewBeforeCoding setting only. (The coder's own gate is
            # broader; see review.state.requires_review_before_coding.)
            return bool(task_metadata.get("requireReviewBeforeCoding", False))
        except (json.JSONDecodeError, OSError) as e:
            # Fail closed for the same reason as above. The broader coder-side
            # gate named in the comment does not rescue this one: a caller that
            # asks this helper and gets False proceeds on that answer.
            _log.warning(
                "[AgentService] Could not read task_metadata for %s (%s); "
                "requiring human review",
                sanitize_log(spec_dir),
                sanitize_log(e),
            )
            return True

    return False


def should_pass_force(spec_dir: Path, force: bool) -> bool:
    """Whether run.py's argv gets ``--force`` for this build (#916).

    The single decision point for BOTH build paths — the in-pod subprocess
    (``agent_service._spawn_task_execution``) and the k8s Job manifest
    (``build_backend.build_run_py_job_manifest``). The kubejob path used to
    hardcode ``--force`` into every manifest, which made the flag a statement
    about nothing rather than about the caller's intent.

    ``--force`` is passed when review is not required (the headless default, and
    what makes unattended builds work) or when the caller explicitly forced it
    (the plan was manually approved via the approve_plan endpoint).
    """
    return force or not sync_require_review_metadata(spec_dir)


def phase_to_status(phase: TaskPhase) -> str:
    """Map execution phase to task status for kanban column placement.

    Every current ``TaskPhase`` member has an entry below, so the fallback
    only fires for a phase this table has never heard of -- a future member
    added to the enum without a matching entry here, or a caller that bypassed
    the enum with a raw string. That used to default to "in_progress", which
    asserts the task is actively moving when nobody has decided that; an
    unmapped phase silently rode into the "still working" kanban column. Route
    it to "human_review" instead -- the same already-accepted status COMPLETED
    and FAILED use to stop and ask a person, rather than inventing progress
    (Factory#431).
    """
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
    status = mapping.get(phase)
    if status is None:
        _log.warning(
            "Unmapped TaskPhase %r has no kanban status; routing to "
            "'human_review' instead of assuming 'in_progress'.",
            phase,
        )
        return "human_review"
    return status


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
