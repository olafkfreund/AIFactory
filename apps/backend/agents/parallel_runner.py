"""
Parallel Phase Runner (#376)
============================

Runs the subtasks of a single ``parallel_safe`` phase concurrently, in
dependency-graph waves, instead of strictly one-at-a-time.

Why this exists
---------------
The serial executor ran every decomposed subtask as its own full agent turn, so
wall-clock and input-token cost scaled linearly with subtask count. Independent
subtasks (e.g. ``app/config.py``, ``tests/test_health.py``, a CI file) have no
data dependency and can run at the same time.

Concurrency safety model
------------------------
Three invariants keep concurrent execution correct on a shared git repository:

1. **Coding is concurrent but isolated.** Each subtask in a wave runs its agent
   session in its *own child git worktree* (branched off the task branch HEAD),
   so two agents never write the same working tree or git index at once. The
   wave scheduler additionally guarantees the subtasks in a wave touch disjoint
   files (see :mod:`implementation_plan.scheduler`).

2. **Merges are sequential.** After a wave's coding finishes, each child branch
   is merged back into the task branch *one at a time*. Concurrent merges would
   race the task branch's index.

3. **Plan state is parent-owned and sequential.** Only this orchestrator (never
   the concurrent child sessions) mutates the canonical
   ``implementation_plan.json``, and only during the sequential post-wave step.

Phases that are not ``parallel_safe`` never reach this module — the caller keeps
using the serial path — so enabling ``--parallel`` is always safe.

Testability
-----------
The three side-effecting steps (run a subtask, merge it, mark it complete) are
injectable callables. The default implementations use the real worktree / SDK /
plan primitives; tests inject fakes to exercise the wave sequencing, merge
ordering, partial-failure handling, and stall detection deterministically.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from implementation_plan.enums import SubtaskStatus
from implementation_plan.scheduler import select_ready_wave, validate_dependencies

logger = logging.getLogger(__name__)


@dataclass
class SubtaskResult:
    """Outcome of running a single subtask's agent session in its worktree."""

    subtask_id: str
    success: bool
    worktree_name: str | None = None
    error: str | None = None
    # Arbitrary extra info a runner wants to pass to the merge step.
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class PhaseRunResult:
    """Aggregate outcome of running one phase in parallel waves."""

    completed_ids: list[str] = field(default_factory=list)
    failed_ids: list[str] = field(default_factory=list)
    stalled: bool = False
    remaining_ids: list[str] = field(default_factory=list)
    waves: int = 0

    @property
    def ok(self) -> bool:
        """True when every subtask completed and nothing stalled/failed."""
        return not self.stalled and not self.failed_ids and not self.remaining_ids


# Type aliases for the injectable hooks.
RunSubtaskFn = Callable[..., Awaitable[SubtaskResult]]
MergeFn = Callable[..., Awaitable[bool]]
MarkCompleteFn = Callable[..., Awaitable[None]]


async def run_parallel_phase(
    subtasks: list[Any],
    *,
    workers: int,
    run_subtask: RunSubtaskFn,
    merge_subtask: MergeFn,
    mark_complete: MarkCompleteFn,
    on_wave: Callable[[int, list[Any]], None] | None = None,
) -> PhaseRunResult:
    """Execute a phase's subtasks concurrently in dependency-graph waves.

    Args:
        subtasks: The phase's subtask/story objects (mutated in place: completed
            ones are marked COMPLETED, failed ones FAILED).
        workers: Maximum subtasks to run concurrently per wave (>= 1).
        run_subtask: ``async (subtask, *, index) -> SubtaskResult`` — runs the
            agent session in an isolated child worktree. MUST NOT touch the
            canonical plan or the task branch (isolation invariant #1).
        merge_subtask: ``async (subtask, result) -> bool`` — merges the child
            branch back into the task branch. Called sequentially (invariant #2).
        mark_complete: ``async (subtask) -> None`` — records completion in the
            canonical plan. Called sequentially (invariant #3).
        on_wave: optional callback ``(wave_index, wave_subtasks)`` for progress
            reporting/logging.

    Returns:
        PhaseRunResult describing what completed, failed, or remains. If the
        graph cannot progress (cycle / unsatisfiable deps / all remaining
        conflict), ``stalled`` is True and ``remaining_ids`` lists the leftover
        subtasks so the caller can fall back to serial execution.
    """
    workers = max(1, int(workers))

    # Advisory DAG validation — never hard-fail a build on a bad plan; the
    # scheduler simply won't mark cyclic/unknown-dep subtasks ready, which
    # surfaces as a stall the caller handles by falling back to serial.
    dep_errors = validate_dependencies(subtasks)
    if dep_errors:
        for err in dep_errors:
            logger.warning("[parallel] dependency issue: %s", err)

    result = PhaseRunResult()
    completed_ids: set[str] = {
        s.id for s in subtasks if getattr(s, "status", None) == SubtaskStatus.COMPLETED
    }
    failed_ids: set[str] = set()

    while True:
        remaining = [
            s
            for s in subtasks
            if s.id not in completed_ids
            and s.id not in failed_ids
            and getattr(s, "status", None) != SubtaskStatus.COMPLETED
        ]
        if not remaining:
            break

        wave = select_ready_wave(
            subtasks, completed_ids, in_flight=[], max_workers=workers
        )
        # Defensive: never re-run something already failed this phase.
        wave = [s for s in wave if s.id not in failed_ids]

        if not wave:
            # Nothing can start: cycle, unknown dependency, or every remaining
            # subtask has an unknown footprint that conflicts. Signal a stall so
            # the caller resumes these serially.
            logger.warning(
                "[parallel] phase stalled with %d subtask(s) remaining: %s",
                len(remaining),
                [s.id for s in remaining],
            )
            result.stalled = True
            result.remaining_ids = [s.id for s in remaining]
            break

        result.waves += 1
        if on_wave:
            on_wave(result.waves, wave)

        # --- Invariant #1: concurrent, ISOLATED coding ----------------------
        coros = [run_subtask(s, index=i) for i, s in enumerate(wave)]
        run_results = await asyncio.gather(*coros, return_exceptions=True)

        # --- Invariants #2 & #3: SEQUENTIAL merge + plan update -------------
        progressed = False
        for subtask, run_res in zip(wave, run_results):
            if isinstance(run_res, BaseException):
                logger.error(
                    "[parallel] subtask %s raised: %s", subtask.id, run_res
                )
                _mark_failed(subtask, failed_ids, str(run_res))
                continue
            if not run_res.success:
                logger.warning(
                    "[parallel] subtask %s session failed: %s",
                    subtask.id,
                    run_res.error,
                )
                _mark_failed(subtask, failed_ids, run_res.error or "session failed")
                continue

            merged = await merge_subtask(subtask, run_res)
            if not merged:
                logger.error(
                    "[parallel] subtask %s merge-back failed", subtask.id
                )
                _mark_failed(subtask, failed_ids, "merge failed")
                continue

            await mark_complete(subtask)
            if hasattr(subtask, "complete"):
                subtask.complete()
            else:  # pragma: no cover - all real subtask types have complete()
                subtask.status = SubtaskStatus.COMPLETED
            completed_ids.add(subtask.id)
            progressed = True

        if not progressed:
            # A whole wave failed without completing anything — avoid an
            # infinite loop; hand the rest back to the serial path.
            logger.error(
                "[parallel] no progress in wave %d; aborting parallel phase",
                result.waves,
            )
            result.stalled = True
            result.remaining_ids = [
                s.id
                for s in subtasks
                if s.id not in completed_ids
                and getattr(s, "status", None) != SubtaskStatus.COMPLETED
            ]
            break

    result.completed_ids = sorted(completed_ids)
    result.failed_ids = sorted(failed_ids)
    return result


def _mark_failed(subtask: Any, failed_ids: set[str], reason: str) -> None:
    """Record a subtask failure (in-memory) without crashing the phase."""
    failed_ids.add(subtask.id)
    if hasattr(subtask, "fail"):
        try:
            subtask.fail(reason)
        except Exception:  # noqa: BLE001 - bookkeeping must not crash a build
            logger.debug("[parallel] could not mark %s failed", subtask.id)


def is_phase_parallel_eligible(phase: Any, workers: int) -> bool:
    """Whether a phase should run via the parallel runner.

    Eligible when the phase is explicitly ``parallel_safe`` and has at least two
    pending subtasks (a single pending subtask gains nothing from a wave).
    """
    if workers <= 1:
        return False
    if not getattr(phase, "parallel_safe", False):
        return False
    pending = [
        s
        for s in getattr(phase, "subtasks", [])
        if getattr(s, "status", None) == SubtaskStatus.PENDING
    ]
    return len(pending) >= 2
