"""
QA Review Re-Drive — Nudge + Human Escalation for Stuck Reviews (#260)
=====================================================================

The foundation (``qa.review_cycle``) can *detect* a ``review_requested`` cycle
that never reached ``review_started`` (an "untouched" review). This module is the
**action** side: when a review is stuck, it drives it forward instead of letting
the task silently stall.

Two-tier strike policy (parameterised, sane defaults):

  * **First strike** — once the request has been untouched for ``timeout_seconds``
    and no strike has been taken yet: deliver an **inbox nudge** to the running
    reviewer (#264 inbox), instructing it to begin the review for the cycle.
  * **Second strike** — if it is STILL untouched after another ``timeout_seconds``
    window since the first nudge: **escalate to a human** by writing the
    control-plane status ``human_review`` with a clear ``reviewReason`` (#259
    control store), so the board surfaces it.

Each strike is recorded atomically on the cycle (``redrive_attempts`` /
``last_redrive_at``) so a poller restart cannot replay strikes and so the
"no double-nudge within a window" guarantee survives a crash.

Dependency direction / single source of truth
----------------------------------------------
This module is deliberately **dependency-injected**: the inbox-enqueue and
control-plane-write functions are passed in. That keeps ``qa/`` free of any
import of the web-server package (the backend runs as a separate subprocess) and
makes the strike logic unit-testable with fakes. The web-server poller wires the
real ``inbox_service.enqueue`` and ``task_control.write_control`` in.

Spec-dir resolution (worktree vs main) — IMPORTANT
--------------------------------------------------
Review-cycle state (``qa_review_cycle.json``) and the control plane
(``task_control.json``) live in the **main** spec dir (never synced from the
agent worktree). But the *running reviewer* drains its inbox from the **build's
worktree** spec dir (see ``apps/backend/agents/inbox.py``). So:

  * detection / strike bookkeeping / escalation  → ``main_spec_dir``
  * inbox nudge delivery                          → ``worktree_spec_dir``

The caller resolves both and passes them explicitly; this module never guesses.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .review_cycle import (
    DEFAULT_UNTOUCHED_TIMEOUT_SECONDS,
    detect_untouched_review,
    record_redrive,
)

# Recipient name for the single running reviewer session. Matches
# ``agents.inbox.DEFAULT_RECIPIENT`` / ``inbox_service.DEFAULT_RECIPIENT``.
REVIEWER_RECIPIENT = "agent"

# Strike at/after which we stop nudging and escalate to a human. With the default
# of 1, strike #1 nudges and strike #2 escalates.
DEFAULT_ESCALATE_AFTER_STRIKES = 1


# Injected writer signatures (documented for callers):
#   EnqueueFn(spec_dir, text, recipient, sender, summary) -> dict
#       e.g. inbox_service.enqueue
#   WriteControlFn(spec_dir, *, status, review_reason, ...) -> dict
#       e.g. task_control.write_control
EnqueueFn = Callable[..., dict[str, Any]]
WriteControlFn = Callable[..., dict[str, Any]]


def _nudge_text(cycle_id: int) -> str:
    return (
        f"Review obligation reminder (cycle {cycle_id}): a peer review was "
        f"requested for this task but has not started yet. Please begin the "
        f"review now — inspect the implementation against the acceptance "
        f"criteria and record your verdict. This is an automated nudge because "
        f"the review request went unacknowledged."
    )


def _escalation_reason(cycle_id: int, strikes: int) -> str:
    return (
        f"Peer review stalled: cycle {cycle_id} was requested but never started "
        f"after {strikes} automated nudge(s). Escalated for human review."
    )


def process_untouched_review(
    main_spec_dir: Path,
    worktree_spec_dir: Path | None,
    *,
    enqueue: EnqueueFn,
    write_control: WriteControlFn,
    timeout_seconds: float = DEFAULT_UNTOUCHED_TIMEOUT_SECONDS,
    escalate_after_strikes: int = DEFAULT_ESCALATE_AFTER_STRIKES,
    recipient: str = REVIEWER_RECIPIENT,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Drive a stuck review forward by exactly one strike, if one is due.

    Idempotent within a window: returns ``None`` (no action) when the review is
    not untouched, the back-off window since the last strike has not elapsed, or
    there is no cycle. Otherwise it performs exactly ONE of:

      * ``"action": "nudged"``     — first strike: inbox nudge delivered.
      * ``"action": "escalated"``  — escalation strike: control-plane set to
        ``human_review``.

    Args:
        main_spec_dir: Main spec dir — authority for cycle + control state.
        worktree_spec_dir: Build's worktree spec dir where the running reviewer
            reads its inbox. Falls back to ``main_spec_dir`` when ``None`` (e.g.
            a non-worktree/local run).
        enqueue: Inbox enqueue function (``inbox_service.enqueue``). Called only
            for the nudge strike.
        write_control: Control-plane writer (``task_control.write_control``).
            Called only for the escalation strike.
        timeout_seconds: Untouched threshold AND the per-strike back-off window.
        escalate_after_strikes: Strikes after which we escalate instead of nudge
            (default 1 → nudge first, escalate second).
        recipient: Inbox recipient (the running reviewer session).
        now: Injectable clock for deterministic tests.

    Returns the action record dict, or ``None`` when nothing was due.
    """
    now = now or datetime.now(UTC)

    # 1. Is the CURRENT cycle an untouched, aged request?
    cycle = detect_untouched_review(
        main_spec_dir, timeout_seconds=timeout_seconds, now=now
    )
    if cycle is None:
        return None

    # 2. Back-off gate: only act once per window. After the first strike we
    #    require another full window before the next one (no double-nudge, and a
    #    real grace period before escalating).
    if not cycle.redrive_window_elapsed(window_seconds=timeout_seconds, now=now):
        return None

    inbox_dir = worktree_spec_dir or main_spec_dir

    # 3. Decide nudge vs escalate from the strike count already taken.
    if cycle.redrive_attempts < escalate_after_strikes:
        # ---- First strike: deliver an inbox nudge to the running reviewer ----
        result = enqueue(
            inbox_dir,
            _nudge_text(cycle.cycle_id),
            recipient,
            "review_obligation",
            f"Begin review (cycle {cycle.cycle_id})",
        )
        # Record the strike AFTER a successful enqueue so a delivery failure
        # doesn't burn the strike (the next poll retries the nudge).
        updated = record_redrive(main_spec_dir, cycle_id=cycle.cycle_id, at=now)
        return {
            "action": "nudged",
            "cycle_id": cycle.cycle_id,
            "recipient": recipient,
            "inbox_dir": str(inbox_dir),
            "delivered": bool(result.get("delivered"))
            if isinstance(result, dict)
            else None,
            "messageId": result.get("messageId") if isinstance(result, dict) else None,
            "redrive_attempts": updated.redrive_attempts,
        }

    # ---- Escalation strike: hand the task to a human via the control plane ----
    strikes_so_far = cycle.redrive_attempts
    reason = _escalation_reason(cycle.cycle_id, strikes_so_far)
    write_control(
        main_spec_dir,
        status="human_review",
        review_reason=reason,
        updated_by="review_redrive",
    )
    updated = record_redrive(main_spec_dir, cycle_id=cycle.cycle_id, at=now)
    return {
        "action": "escalated",
        "cycle_id": cycle.cycle_id,
        "status": "human_review",
        "reviewReason": reason,
        "redrive_attempts": updated.redrive_attempts,
    }


__all__ = [
    "DEFAULT_ESCALATE_AFTER_STRIKES",
    "REVIEWER_RECIPIENT",
    "process_untouched_review",
]
