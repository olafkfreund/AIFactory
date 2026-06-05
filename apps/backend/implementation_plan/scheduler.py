#!/usr/bin/env python3
"""
Wave Scheduler (#376)
=====================

Pure, side-effect-free scheduling logic for running independent subtasks
concurrently instead of one-at-a-time.

The executor historically ran every decomposed subtask serially (one full agent
turn each), so wall-clock and input-token cost scaled linearly with subtask
count. This module computes which subtasks may safely run *together* in a
"wave", given:

1. **Dependency order** - a subtask only becomes ready once every id in its
   ``depends_on`` list has completed.
2. **File-set disjointness** - two subtasks may run concurrently only if the
   files they create/modify do not overlap (they would otherwise race on the
   working tree / git index). A subtask with an *unknown* file set (empty)
   is treated conservatively as conflicting with everything, so it runs solo.
3. **Worker cap** - at most ``max_workers`` subtasks run at once.

The functions here are deliberately decoupled from :class:`ImplementationPlan`
and from any I/O: they accept plain subtask-like objects (duck-typed: any object
exposing ``id``, ``status``, ``depends_on``, ``files_to_create`` and
``files_to_modify``). This keeps the wave logic trivially unit-testable. The
executor integration layer is responsible for selecting which phase's subtasks
to feed in and for actually running the returned wave.
"""

from __future__ import annotations

import os
from typing import Any, Protocol

from .enums import SubtaskStatus


class SubtaskLike(Protocol):
    """Structural type for anything the scheduler can reason about.

    Both :class:`~implementation_plan.subtask.Subtask` and
    :class:`~implementation_plan.story.Story` satisfy this protocol.
    """

    id: str
    status: SubtaskStatus
    depends_on: list[str]
    files_to_create: list[str]
    files_to_modify: list[str]


def _norm_path(path: str) -> str:
    """Normalize a path for overlap comparison.

    Strips whitespace and a leading ``./`` and collapses ``..``/redundant
    separators so that e.g. ``./app/config.py`` and ``app/config.py`` compare
    equal. Paths are NOT lower-cased (Linux filesystems are case-sensitive).
    """
    cleaned = path.strip()
    if not cleaned:
        return ""
    return os.path.normpath(cleaned)


def file_set(subtask: Any) -> set[str]:
    """Return the normalized set of files a subtask creates or modifies.

    An empty set means the subtask's footprint is unknown (e.g. an
    investigation/research subtask that declared no files). Callers treat an
    empty footprint as "conflicts with everything" so such subtasks run alone.
    """
    created = getattr(subtask, "files_to_create", None) or []
    modified = getattr(subtask, "files_to_modify", None) or []
    return {p for p in (_norm_path(f) for f in (*created, *modified)) if p}


def _depends_on(subtask: Any) -> list[str]:
    """Return a subtask's dependency ids, tolerating objects without the field."""
    return list(getattr(subtask, "depends_on", None) or [])


def conflicts(a: Any, b: Any) -> bool:
    """Whether two subtasks cannot safely run at the same time.

    They conflict if their file sets overlap, or if *either* footprint is
    unknown (empty) — in which case we conservatively refuse to co-schedule.
    """
    fa = file_set(a)
    fb = file_set(b)
    if not fa or not fb:
        return True
    return not fa.isdisjoint(fb)


def validate_dependencies(subtasks: list[Any]) -> list[str]:
    """Validate the dependency graph; return a list of human-readable errors.

    Detects:
      * references to unknown subtask ids
      * self-dependencies
      * dependency cycles

    An empty list means the graph is a valid DAG. This is advisory: the executor
    can log warnings and fall back to serial execution rather than hard-failing,
    so a bad planner output never bricks a build.
    """
    errors: list[str] = []
    ids = {getattr(s, "id", None) for s in subtasks}
    by_id: dict[str, Any] = {s.id: s for s in subtasks if getattr(s, "id", None)}

    # Unknown references + self-deps
    for s in subtasks:
        for dep in _depends_on(s):
            if dep == s.id:
                errors.append(f"Subtask '{s.id}' depends on itself")
            elif dep not in ids:
                errors.append(
                    f"Subtask '{s.id}' depends on unknown subtask '{dep}'"
                )

    # Cycle detection via DFS over known edges (ignore unknown deps already flagged)
    WHITE, GREY, BLACK = 0, 1, 2
    color: dict[str, int] = {sid: WHITE for sid in by_id}

    def visit(node: str, stack: list[str]) -> None:
        color[node] = GREY
        stack.append(node)
        for dep in _depends_on(by_id[node]):
            if dep not in by_id:
                continue  # unknown dep already reported
            if color[dep] == GREY:
                cycle = stack[stack.index(dep):] + [dep]
                errors.append("Dependency cycle: " + " -> ".join(cycle))
            elif color[dep] == WHITE:
                visit(dep, stack)
        stack.pop()
        color[node] = BLACK

    for sid in by_id:
        if color[sid] == WHITE:
            visit(sid, [])

    # De-duplicate while preserving order (a cycle is found once per entry node)
    seen: set[str] = set()
    deduped = []
    for e in errors:
        if e not in seen:
            seen.add(e)
            deduped.append(e)
    return deduped


def select_ready_wave(
    subtasks: list[Any],
    completed_ids: set[str],
    in_flight: list[Any] | None = None,
    max_workers: int = 1,
) -> list[Any]:
    """Select the next wave of subtasks to start concurrently.

    Args:
        subtasks: Candidate subtasks (typically one phase's subtasks). Order is
            preserved — earlier subtasks win ties, giving deterministic waves.
        completed_ids: Ids of subtasks already finished (dependency satisfaction).
        in_flight: Subtasks currently running (their file sets reserve those
            paths, and they count against ``max_workers``).
        max_workers: Maximum number of subtasks allowed to run at once
            (in-flight + newly selected). Values < 1 are treated as 1.

    Returns:
        The subtasks to launch now (possibly empty if nothing is ready or all
        free slots conflict). An empty result with pending-but-not-ready
        subtasks and no in-flight work signals a stall (cycle / missing dep) —
        the caller should detect and handle that.
    """
    in_flight = in_flight or []
    max_workers = max(1, int(max_workers))

    free_slots = max_workers - len(in_flight)
    if free_slots <= 0:
        return []

    # Reserved footprints: in-flight subtasks hold their files for the wave.
    reserved: list[Any] = list(in_flight)
    picked: list[Any] = []

    for s in subtasks:
        if len(picked) >= free_slots:
            break
        if getattr(s, "status", None) != SubtaskStatus.PENDING:
            continue
        if s.id in completed_ids:
            continue
        if any(s.id == getattr(r, "id", None) for r in reserved):
            continue  # already running
        # Dependencies must all be complete.
        if not all(dep in completed_ids for dep in _depends_on(s)):
            continue
        # Must not conflict with anything already reserved this wave.
        if any(conflicts(s, r) for r in reserved):
            continue
        picked.append(s)
        reserved.append(s)

    return picked
