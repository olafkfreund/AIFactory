#!/usr/bin/env python3
"""
Tests for QA Review Re-Drive: nudge + human escalation (#260)
=============================================================

Covers the delivery slice that completes #260:

- untouched review → FIRST strike enqueues an inbox nudge (asserted against the
  real inbox store) and increments the persisted strike counter
- still untouched after another window → SECOND strike escalates to the
  control-plane ``human_review`` status with a clear reviewReason
- strike counter increments and PERSISTS in qa_review_cycle.json
- no double-nudge within a back-off window (exactly-once per window)
- ``now`` injected to drive timeouts deterministically
- nudge is delivered to the WORKTREE spec dir's inbox, while cycle/control
  state lives in the MAIN spec dir

The re-drive orchestrator (``qa.review_redrive``) is dependency-injected, so we
wire the REAL web-server ``inbox_service.enqueue`` and ``task_control.write_control``
to exercise the true on-disk effects.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# Both apps participate: backend orchestrator + web-server writers.
_BACKEND = Path(__file__).parent.parent / "apps" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
_WEB_SERVER = Path(__file__).parent.parent / "apps" / "web-server"
if str(_WEB_SERVER) not in sys.path:
    sys.path.insert(0, str(_WEB_SERVER))

from qa.review_cycle import (  # noqa: E402
    CycleState,
    load_cycle,
    record_started,
    request_review,
)
from qa.review_redrive import process_untouched_review  # noqa: E402
from server.services import inbox_service, task_control  # noqa: E402

TIMEOUT = 300.0


@pytest.fixture
def dirs(tmp_path):
    """Return (main_spec_dir, worktree_spec_dir), both created."""
    main = tmp_path / "specs" / "001-feature"
    worktree = (
        tmp_path / "worktrees" / "tasks" / "001-feature" / "specs" / "001-feature"
    )
    main.mkdir(parents=True)
    worktree.mkdir(parents=True)
    return main, worktree


def _aged(seconds: float) -> datetime:
    """A clock value ``seconds`` after now (to drive timeouts)."""
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


# =============================================================================
# FIRST STRIKE — inbox nudge
# =============================================================================


class TestFirstStrikeNudge:
    def test_untouched_request_enqueues_inbox_nudge(self, dirs):
        main, worktree = dirs
        cycle = request_review(main)

        result = process_untouched_review(
            main,
            worktree,
            enqueue=inbox_service.enqueue,
            write_control=task_control.write_control,
            timeout_seconds=TIMEOUT,
            now=_aged(TIMEOUT + 1),
        )

        assert result is not None
        assert result["action"] == "nudged"
        assert result["cycle_id"] == cycle.cycle_id
        assert result["delivered"] is True

        # The nudge landed in the WORKTREE inbox (where the reviewer reads),
        # NOT the main spec dir.
        msgs = inbox_service.list_messages(worktree, recipient="agent")
        assert len(msgs) == 1
        assert str(cycle.cycle_id) in msgs[0]["text"]
        assert not (main / "inbox").exists()

    def test_strike_counter_persists(self, dirs):
        main, worktree = dirs
        request_review(main)
        process_untouched_review(
            main,
            worktree,
            enqueue=inbox_service.enqueue,
            write_control=task_control.write_control,
            timeout_seconds=TIMEOUT,
            now=_aged(TIMEOUT + 1),
        )
        reloaded = load_cycle(main)
        assert reloaded.redrive_attempts == 1
        assert reloaded.last_redrive_at is not None
        # Strike state survives a fresh read (single source of truth on disk).
        raw = json.loads((main / "qa_review_cycle.json").read_text())
        assert raw["redrive_attempts"] == 1

    def test_no_action_before_timeout(self, dirs):
        main, worktree = dirs
        request_review(main)
        # now is only +10s; threshold is 300s → not yet untouched.
        result = process_untouched_review(
            main,
            worktree,
            enqueue=inbox_service.enqueue,
            write_control=task_control.write_control,
            timeout_seconds=TIMEOUT,
            now=_aged(10),
        )
        assert result is None
        assert inbox_service.list_messages(worktree, recipient="agent") == []

    def test_started_review_is_never_nudged(self, dirs):
        main, worktree = dirs
        c = request_review(main)
        record_started(main, cycle_id=c.cycle_id)  # reviewer engaged
        result = process_untouched_review(
            main,
            worktree,
            enqueue=inbox_service.enqueue,
            write_control=task_control.write_control,
            timeout_seconds=TIMEOUT,
            now=_aged(TIMEOUT * 10),
        )
        assert result is None
        assert inbox_service.list_messages(worktree, recipient="agent") == []


# =============================================================================
# NO DOUBLE-NUDGE WITHIN A WINDOW
# =============================================================================


class TestNoDoubleNudge:
    def test_second_call_same_window_is_noop(self, dirs):
        main, worktree = dirs
        request_review(main)
        t1 = _aged(TIMEOUT + 1)
        first = process_untouched_review(
            main,
            worktree,
            enqueue=inbox_service.enqueue,
            write_control=task_control.write_control,
            timeout_seconds=TIMEOUT,
            now=t1,
        )
        assert first["action"] == "nudged"

        # A second poll a few seconds later (same window) does nothing.
        second = process_untouched_review(
            main,
            worktree,
            enqueue=inbox_service.enqueue,
            write_control=task_control.write_control,
            timeout_seconds=TIMEOUT,
            now=t1 + timedelta(seconds=5),
        )
        assert second is None
        # Still exactly one nudge, still one strike.
        assert len(inbox_service.list_messages(worktree, recipient="agent")) == 1
        assert load_cycle(main).redrive_attempts == 1


# =============================================================================
# SECOND STRIKE — human escalation
# =============================================================================


class TestSecondStrikeEscalation:
    def test_second_window_escalates_to_human_review(self, dirs):
        main, worktree = dirs
        cycle = request_review(main)
        t1 = _aged(TIMEOUT + 1)
        # First strike: nudge.
        r1 = process_untouched_review(
            main,
            worktree,
            enqueue=inbox_service.enqueue,
            write_control=task_control.write_control,
            timeout_seconds=TIMEOUT,
            now=t1,
        )
        assert r1["action"] == "nudged"

        # Second strike: another full window later, still untouched → escalate.
        t2 = t1 + timedelta(seconds=TIMEOUT + 1)
        r2 = process_untouched_review(
            main,
            worktree,
            enqueue=inbox_service.enqueue,
            write_control=task_control.write_control,
            timeout_seconds=TIMEOUT,
            now=t2,
        )
        assert r2 is not None
        assert r2["action"] == "escalated"
        assert r2["status"] == "human_review"
        assert str(cycle.cycle_id) in r2["reviewReason"]

        # Control plane really shows human_review with a reason.
        ctrl = task_control.read_control(main)
        assert ctrl["status"] == "human_review"
        assert "stalled" in ctrl["reviewReason"].lower()

        # Two strikes now recorded; no second inbox message was sent.
        assert load_cycle(main).redrive_attempts == 2
        assert len(inbox_service.list_messages(worktree, recipient="agent")) == 1

    def test_escalate_after_zero_strikes_when_configured(self, dirs):
        # escalate_after_strikes=0 → escalate immediately on first untouched hit.
        main, worktree = dirs
        request_review(main)
        result = process_untouched_review(
            main,
            worktree,
            enqueue=inbox_service.enqueue,
            write_control=task_control.write_control,
            timeout_seconds=TIMEOUT,
            escalate_after_strikes=0,
            now=_aged(TIMEOUT + 1),
        )
        assert result["action"] == "escalated"
        assert task_control.read_control(main)["status"] == "human_review"
        assert inbox_service.list_messages(worktree, recipient="agent") == []


# =============================================================================
# SPEC-DIR ROUTING + ROBUSTNESS
# =============================================================================


class TestRouting:
    def test_falls_back_to_main_spec_dir_for_inbox(self, dirs):
        # When no worktree dir is supplied, the nudge goes to the main spec dir.
        main, _ = dirs
        request_review(main)
        result = process_untouched_review(
            main,
            None,
            enqueue=inbox_service.enqueue,
            write_control=task_control.write_control,
            timeout_seconds=TIMEOUT,
            now=_aged(TIMEOUT + 1),
        )
        assert result["action"] == "nudged"
        assert len(inbox_service.list_messages(main, recipient="agent")) == 1

    def test_no_cycle_is_noop(self, dirs):
        main, worktree = dirs
        # No request_review() called → no cycle file.
        result = process_untouched_review(
            main,
            worktree,
            enqueue=inbox_service.enqueue,
            write_control=task_control.write_control,
            timeout_seconds=TIMEOUT,
            now=_aged(TIMEOUT + 1),
        )
        assert result is None

    def test_failed_enqueue_does_not_burn_strike(self, dirs):
        main, worktree = dirs
        request_review(main)

        def boom(*_a, **_k):
            raise RuntimeError("inbox down")

        with pytest.raises(RuntimeError):
            process_untouched_review(
                main,
                worktree,
                enqueue=boom,
                write_control=task_control.write_control,
                timeout_seconds=TIMEOUT,
                now=_aged(TIMEOUT + 1),
            )
        # Strike NOT recorded because enqueue failed before record_redrive.
        assert load_cycle(main).redrive_attempts == 0
