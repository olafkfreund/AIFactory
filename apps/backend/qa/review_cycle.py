"""
QA Review-Cycle State — Peer-Review Obligation Tracking (#260)
=============================================================

AIFactory's QA loop (``qa_reviewer`` → ``qa_fixer``) implicitly assumes that a
*requested* review actually *happened*. It does not. If a review is requested
but the reviewer never engages — the agent crashes, the provider stalls, the
session never starts — the task can silently stall with no evidence that the
reviewer ever looked at anything. The lesson (reimplemented clean, not copied):
**inbox-insertion / review-request is NOT proof the review happened**; you need
evidence the reviewer ENGAGED, and you must never let evidence from an OLD
review cycle satisfy a NEW request.

This module models an explicit review cycle with strict boundaries::

    review_requested → review_started → (approved | changes_requested)

and persists it atomically, keyed per task, with a **monotonic cycle id**.

Why a separate file (not ``implementation_plan.json``)?
    Iteration history already lives in ``implementation_plan.json`` — the file
    the QA *agent* rewrites in its isolated worktree. Storing the review-cycle
    there would let a worktree sync clobber or replay it (the exact cross-cycle
    drift bug we are guarding against). Following the #259 ``task_control.py``
    rationale, review-cycle state gets a dedicated home,
    ``<spec_dir>/qa_review_cycle.json``, that:

      * is written ONLY by the QA loop (the single authority), never by agents,
      * is NEVER part of any worktree-sync file list,
      * is written atomically (tmp file + ``os.replace``) — mirroring the #264
        inbox delivery-proof pattern — so a crash mid-write can't corrupt it,
      * carries a monotonic ``cycle_id`` so engagement proof recorded against an
        old cycle can never be mixed into a freshly-requested cycle.

Why ``qa/`` and not ``task_control.py``?
    This is QA-domain lifecycle state, produced and consumed entirely by the QA
    loop. ``task_control.py`` owns *control-plane* state set by humans / the
    web-server (board column, human reviewReason). Keeping the two separate
    preserves each module's single-authority guarantee: the web-server never
    writes the QA cycle, the QA loop never writes the control plane.

Single source of truth
-----------------------
The persisted ``qa_review_cycle.json`` is the ONLY authority for review-cycle
state. The QA loop records ``request → start → resolve`` exclusively through
this module; nothing else (loop locals, UI timers, the plan file) may hold a
competing copy. Reads always come from disk so every caller sees the same state.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

# Dedicated review-cycle file. Lives in the *main* spec dir alongside
# implementation_plan.json, and is deliberately excluded from every
# worktree-sync file list so agents can never write or replay it.
REVIEW_CYCLE_FILE = "qa_review_cycle.json"

# Default bound (seconds) after which a `review_requested` that never reached
# `review_started` is considered "untouched" and eligible for re-drive.
DEFAULT_UNTOUCHED_TIMEOUT_SECONDS = 300.0


class CycleState(str, Enum):
    """The strict review-cycle lifecycle.

    Transitions are one-directional within a cycle:

        REQUESTED → STARTED → (APPROVED | CHANGES_REQUESTED)

    A brand-new cycle (a new `review_requested`) always increments the
    monotonic ``cycle_id``, so no terminal-state evidence from a prior cycle
    can be reinterpreted as satisfying the new request.
    """

    REQUESTED = "review_requested"
    STARTED = "review_started"
    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"


# Allowed forward transitions. Anything not listed here is rejected so the
# lifecycle can never skip the engagement-proof step or move backwards.
_ALLOWED_TRANSITIONS: dict[CycleState, set[CycleState]] = {
    CycleState.REQUESTED: {CycleState.STARTED},
    CycleState.STARTED: {CycleState.APPROVED, CycleState.CHANGES_REQUESTED},
    CycleState.APPROVED: set(),
    CycleState.CHANGES_REQUESTED: set(),
}

_TERMINAL_STATES = {CycleState.APPROVED, CycleState.CHANGES_REQUESTED}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class ReviewCycleError(Exception):
    """Base error for review-cycle operations."""


class InvalidTransitionError(ReviewCycleError):
    """Raised when a state transition violates the strict lifecycle."""


class StaleCycleError(ReviewCycleError):
    """Raised when an action targets a cycle id that is no longer current.

    This is the cross-cycle guard: evidence (e.g. ``review_started``) carrying
    an old ``cycle_id`` is rejected against the current, newer cycle instead of
    being silently mixed in.
    """


@dataclass
class EngagementProof:
    """Evidence that the reviewer actually STARTED — not merely that a request
    was written.

    What counts as engagement: the reviewer agent emitting its first concrete
    action for *this* cycle. Concretely the QA loop records a proof when the
    reviewer session produces a tool call or assistant output (``marker`` =
    ``"reviewer_first_action"``), or when a session explicitly emits a
    ``review_started`` marker. A bare request with no session output never
    produces a proof, so it remains detectably untouched.

    ``cycle_id`` binds the proof to the exact cycle it belongs to; a proof whose
    ``cycle_id`` does not match the current cycle is refused (see
    :meth:`ReviewCycle.record_started`).
    """

    cycle_id: int
    marker: str
    recorded_at: str = field(default_factory=_now_iso)
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EngagementProof:
        return cls(
            cycle_id=int(data["cycle_id"]),
            marker=str(data.get("marker", "")),
            recorded_at=str(data.get("recorded_at", _now_iso())),
            detail=dict(data.get("detail", {})),
        )


@dataclass
class ReviewCycle:
    """The persisted review-cycle state for a single spec/task.

    Holds the monotonic ``cycle_id``, the current ``state``, timing for
    untouched-review detection, the engagement proof for the current cycle, and
    an append-only ``resolutions`` log so each cycle resolves exactly once.
    """

    cycle_id: int = 0
    state: CycleState = CycleState.REQUESTED
    requested_at: str = field(default_factory=_now_iso)
    started_at: str | None = None
    resolved_at: str | None = None
    proof: EngagementProof | None = None
    # Append-only audit of terminal resolutions, one entry per resolved cycle.
    resolutions: list[dict[str, Any]] = field(default_factory=list)
    # Re-drive (nudge/escalation) bookkeeping for an untouched request. Persisted
    # so a poller restart can't replay strikes and so "no double-nudge within a
    # window" survives a crash. ``redrive_attempts`` counts strikes already taken
    # against THIS cycle; ``last_redrive_at`` gates the next window.
    redrive_attempts: int = 0
    last_redrive_at: str | None = None
    updated_at: str = field(default_factory=_now_iso)

    # ------------------------------------------------------------------ #
    # Serialization
    # ------------------------------------------------------------------ #
    def to_dict(self) -> dict[str, Any]:
        return {
            "cycle_id": self.cycle_id,
            "state": self.state.value,
            "requested_at": self.requested_at,
            "started_at": self.started_at,
            "resolved_at": self.resolved_at,
            "proof": self.proof.to_dict() if self.proof else None,
            "resolutions": self.resolutions,
            "redrive_attempts": self.redrive_attempts,
            "last_redrive_at": self.last_redrive_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReviewCycle:
        proof_data = data.get("proof")
        return cls(
            cycle_id=int(data.get("cycle_id", 0)),
            state=CycleState(data.get("state", CycleState.REQUESTED.value)),
            requested_at=str(data.get("requested_at", _now_iso())),
            started_at=data.get("started_at"),
            resolved_at=data.get("resolved_at"),
            proof=EngagementProof.from_dict(proof_data) if proof_data else None,
            resolutions=list(data.get("resolutions", [])),
            redrive_attempts=int(data.get("redrive_attempts", 0)),
            last_redrive_at=data.get("last_redrive_at"),
            updated_at=str(data.get("updated_at", _now_iso())),
        )

    # ------------------------------------------------------------------ #
    # Queries
    # ------------------------------------------------------------------ #
    def is_started(self) -> bool:
        """True once the reviewer has provably engaged with the current cycle."""
        return self.state in (
            CycleState.STARTED,
            CycleState.APPROVED,
            CycleState.CHANGES_REQUESTED,
        )

    def is_resolved(self) -> bool:
        """True once the current cycle has reached a terminal state."""
        return self.state in _TERMINAL_STATES

    def is_untouched(
        self, *, timeout_seconds: float, now: datetime | None = None
    ) -> bool:
        """Detect a ``review_requested`` that never transitioned to
        ``review_started`` within ``timeout_seconds``.

        Returns True only when the cycle is still in ``REQUESTED`` (no
        engagement proof) AND more than ``timeout_seconds`` have elapsed since
        the request was written. A started or resolved cycle is never
        "untouched".
        """
        if self.state != CycleState.REQUESTED:
            return False
        now = now or datetime.now(UTC)
        try:
            requested = datetime.fromisoformat(self.requested_at)
        except (TypeError, ValueError):
            return False
        if requested.tzinfo is None:
            requested = requested.replace(tzinfo=UTC)
        return (now - requested).total_seconds() >= timeout_seconds

    def redrive_window_elapsed(
        self, *, window_seconds: float, now: datetime | None = None
    ) -> bool:
        """True when at least ``window_seconds`` have passed since the last
        re-drive (or since the request, if no re-drive has happened yet).

        This is the "no double-nudge within a window" gate: a strike is only
        eligible once a full window has elapsed since the previous one.
        """
        now = now or datetime.now(UTC)
        anchor_iso = self.last_redrive_at or self.requested_at
        try:
            anchor = datetime.fromisoformat(anchor_iso)
        except (TypeError, ValueError):
            return False
        if anchor.tzinfo is None:
            anchor = anchor.replace(tzinfo=UTC)
        return (now - anchor).total_seconds() >= window_seconds


# ====================================================================== #
# PERSISTENCE
# ====================================================================== #


def cycle_file_path(spec_dir: Path) -> Path:
    """Return the review-cycle file path for a spec dir."""
    return Path(spec_dir) / REVIEW_CYCLE_FILE


def load_cycle(spec_dir: Path) -> ReviewCycle | None:
    """Load the persisted review cycle, or None if none exists / is unreadable.

    Reads always come from disk so every caller observes the single source of
    truth (no in-memory cache that could drift).
    """
    cfile = cycle_file_path(spec_dir)
    if not cfile.exists():
        return None
    try:
        data = json.loads(cfile.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        return ReviewCycle.from_dict(data)
    except (KeyError, ValueError):
        return None


def _atomic_write_cycle(spec_dir: Path, cycle: ReviewCycle) -> None:
    """Atomically persist the review cycle (tmp file + ``os.replace``).

    Mirrors the #259/#264 atomic-write pattern: a temp file in the same
    directory is fsync'd and then atomically renamed over the target, so a
    reader never observes a half-written file and a crash leaves the previous
    good state intact.
    """
    path = cycle_file_path(spec_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    cycle.updated_at = _now_iso()
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(cycle.to_dict(), fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


# ====================================================================== #
# LIFECYCLE OPERATIONS (the single authority for review-cycle transitions)
# ====================================================================== #


def request_review(spec_dir: Path) -> ReviewCycle:
    """Open a NEW review cycle (``review_requested``).

    A new request ALWAYS increments the monotonic ``cycle_id`` (relative to any
    prior persisted cycle). This is the cross-cycle guard: because the new cycle
    has a strictly higher id and a fresh (empty) proof, no ``review_started``
    evidence recorded against an earlier cycle can satisfy this request.

    Idempotency note: each call begins a distinct cycle. Callers that merely
    want the current cycle should use :func:`load_cycle`.
    """
    previous = load_cycle(spec_dir)
    next_id = (previous.cycle_id + 1) if previous else 1
    cycle = ReviewCycle(
        cycle_id=next_id,
        state=CycleState.REQUESTED,
        requested_at=_now_iso(),
        # Carry the resolution audit forward so history is not lost across cycles.
        resolutions=list(previous.resolutions) if previous else [],
    )
    _atomic_write_cycle(spec_dir, cycle)
    return cycle


def record_started(
    spec_dir: Path,
    *,
    cycle_id: int,
    marker: str = "reviewer_first_action",
    detail: dict[str, Any] | None = None,
) -> ReviewCycle:
    """Record engagement proof that the reviewer STARTED the given cycle.

    ``cycle_id`` MUST match the current persisted cycle. If it does not — e.g.
    a stale reviewer session reports a ``review_started`` for an already-superseded
    cycle — :class:`StaleCycleError` is raised and the proof is rejected, so old
    evidence can never be mixed into a newer ``review_requested``.

    This is what flips ``review_requested → review_started``: a bare request
    with no recorded proof stays detectably untouched.
    """
    cycle = load_cycle(spec_dir)
    if cycle is None:
        raise ReviewCycleError("No review cycle exists; call request_review first")
    if cycle_id != cycle.cycle_id:
        raise StaleCycleError(
            f"Engagement proof for cycle {cycle_id} rejected: current cycle is "
            f"{cycle.cycle_id} (cross-cycle evidence is not allowed)"
        )
    if cycle.state == CycleState.REQUESTED:
        _ensure_transition(cycle.state, CycleState.STARTED)
        cycle.state = CycleState.STARTED
        cycle.started_at = _now_iso()
    # If already STARTED (or terminal) we keep the first proof and timestamp;
    # re-reporting engagement for the same cycle is a no-op, not an error.
    if cycle.proof is None:
        cycle.proof = EngagementProof(
            cycle_id=cycle.cycle_id,
            marker=marker,
            detail=detail or {},
        )
    _atomic_write_cycle(spec_dir, cycle)
    return cycle


def resolve_review(
    spec_dir: Path,
    *,
    cycle_id: int,
    approved: bool,
    detail: dict[str, Any] | None = None,
) -> ReviewCycle:
    """Resolve the current cycle to ``approved`` or ``changes_requested``.

    Requires the cycle to have provably STARTED (engagement proof present);
    resolving a cycle that was never started is an :class:`InvalidTransitionError`,
    enforcing the "the review must actually have happened" obligation.

    Exactly-once: resolving an already-terminal cycle raises
    :class:`InvalidTransitionError`. Each resolution appends one entry to the
    append-only ``resolutions`` log keyed by ``cycle_id``.
    """
    cycle = load_cycle(spec_dir)
    if cycle is None:
        raise ReviewCycleError("No review cycle exists; call request_review first")
    if cycle_id != cycle.cycle_id:
        raise StaleCycleError(
            f"Resolution for cycle {cycle_id} rejected: current cycle is "
            f"{cycle.cycle_id}"
        )
    target = CycleState.APPROVED if approved else CycleState.CHANGES_REQUESTED
    _ensure_transition(cycle.state, target)  # rejects un-started or already-terminal
    cycle.state = target
    cycle.resolved_at = _now_iso()
    cycle.resolutions.append(
        {
            "cycle_id": cycle.cycle_id,
            "outcome": target.value,
            "resolved_at": cycle.resolved_at,
            "detail": detail or {},
        }
    )
    _atomic_write_cycle(spec_dir, cycle)
    return cycle


def record_redrive(
    spec_dir: Path,
    *,
    cycle_id: int,
    at: datetime | None = None,
) -> ReviewCycle:
    """Record a re-drive strike (nudge or escalation) against the current cycle.

    Increments ``redrive_attempts`` and stamps ``last_redrive_at`` atomically, so
    the strike count and window gate are persisted in the single source of truth
    and survive a poller restart (no replayed strikes).

    Rejects a stale ``cycle_id`` (cross-cycle guard) just like the other ops, so
    a strike can never be attributed to a superseded cycle.
    """
    cycle = load_cycle(spec_dir)
    if cycle is None:
        raise ReviewCycleError("No review cycle exists; call request_review first")
    if cycle_id != cycle.cycle_id:
        raise StaleCycleError(
            f"Re-drive for cycle {cycle_id} rejected: current cycle is {cycle.cycle_id}"
        )
    cycle.redrive_attempts += 1
    cycle.last_redrive_at = (at or datetime.now(UTC)).isoformat()
    _atomic_write_cycle(spec_dir, cycle)
    return cycle


def _ensure_transition(current: CycleState, target: CycleState) -> None:
    """Validate ``current → target`` against the strict lifecycle."""
    if target not in _ALLOWED_TRANSITIONS.get(current, set()):
        raise InvalidTransitionError(
            f"Illegal review-cycle transition {current.value} → {target.value}"
        )


# ====================================================================== #
# UNTOUCHED-REVIEW DETECTION + RE-DRIVE / ESCALATION HOOK
# ====================================================================== #


def detect_untouched_review(
    spec_dir: Path,
    *,
    timeout_seconds: float = DEFAULT_UNTOUCHED_TIMEOUT_SECONDS,
    now: datetime | None = None,
) -> ReviewCycle | None:
    """Return the current cycle iff it is an untouched ``review_requested``.

    "Untouched" = still in ``REQUESTED`` (no engagement proof) and older than
    ``timeout_seconds``. Returns ``None`` when there is no cycle, the cycle has
    started, or the timeout has not yet elapsed — so callers can poll this every
    loop tick and act only when a review has provably stalled.
    """
    cycle = load_cycle(spec_dir)
    if cycle is None:
        return None
    if cycle.is_untouched(timeout_seconds=timeout_seconds, now=now):
        return cycle
    return None


def redrive_untouched_review(
    spec_dir: Path,
    *,
    timeout_seconds: float = DEFAULT_UNTOUCHED_TIMEOUT_SECONDS,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Detect an untouched review and emit a re-drive/escalation signal.

    Returns a structured signal dict when a stalled review is found (so the
    caller can nudge the reviewer or escalate), or ``None`` when nothing is
    stalled. This is the pure detection/decision payload; the actual *delivery*
    (inbox nudge + human escalation, with strike tracking) is orchestrated by
    :func:`qa.review_redrive.process_untouched_review`, which injects the inbox
    and control-plane writers so this state-only module stays dependency-free.
    """
    cycle = detect_untouched_review(spec_dir, timeout_seconds=timeout_seconds, now=now)
    if cycle is None:
        return None

    signal = {
        "action": "redrive_review",
        "cycle_id": cycle.cycle_id,
        "state": cycle.state.value,
        "requested_at": cycle.requested_at,
        "timeout_seconds": timeout_seconds,
        "redrive_attempts": cycle.redrive_attempts,
        "reason": (
            f"Review cycle {cycle.cycle_id} was requested at {cycle.requested_at} "
            f"but never reached review_started within {timeout_seconds:.0f}s."
        ),
    }
    return signal


__all__ = [
    "DEFAULT_UNTOUCHED_TIMEOUT_SECONDS",
    "REVIEW_CYCLE_FILE",
    "CycleState",
    "EngagementProof",
    "InvalidTransitionError",
    "ReviewCycle",
    "ReviewCycleError",
    "StaleCycleError",
    "cycle_file_path",
    "detect_untouched_review",
    "load_cycle",
    "record_redrive",
    "record_started",
    "redrive_untouched_review",
    "request_review",
    "resolve_review",
]
