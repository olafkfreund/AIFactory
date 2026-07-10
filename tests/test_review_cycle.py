#!/usr/bin/env python3
"""
Tests for QA Review-Cycle Obligation Tracking (#260)
====================================================

Covers the foundation contract of ``apps/backend/qa/review_cycle.py``:

- cycle transitions (requested → started → approved | changes_requested)
- the strict-boundary guard (engagement evidence from an OLD cycle is
  rejected against a NEW review_requested via the monotonic cycle id)
- untouched-review detection (a requested cycle that never started)
- exactly-once resolution (a terminal cycle cannot be resolved twice)

The module is dependency-free (stdlib only), so it imports directly; conftest
already puts ``apps/backend`` on ``sys.path``.
"""

from datetime import datetime, timedelta, timezone

import pytest
from qa.review_cycle import (
    CycleState,
    InvalidTransitionError,
    ReviewCycleError,
    StaleCycleError,
    cycle_file_path,
    detect_untouched_review,
    load_cycle,
    record_redrive,
    record_started,
    redrive_untouched_review,
    request_review,
    resolve_review,
)


@pytest.fixture
def spec_dir(tmp_path):
    """A fresh, empty spec directory for each test."""
    d = tmp_path / "001-feature"
    d.mkdir()
    return d


# =============================================================================
# HAPPY-PATH TRANSITIONS
# =============================================================================


class TestTransitions:
    def test_request_creates_cycle_one(self, spec_dir):
        cycle = request_review(spec_dir)
        assert cycle.cycle_id == 1
        assert cycle.state is CycleState.REQUESTED
        assert cycle.proof is None
        assert cycle_file_path(spec_dir).exists()

    def test_requested_to_started_to_approved(self, spec_dir):
        c1 = request_review(spec_dir)
        started = record_started(spec_dir, cycle_id=c1.cycle_id)
        assert started.state is CycleState.STARTED
        assert started.proof is not None
        assert started.proof.cycle_id == c1.cycle_id
        assert started.started_at is not None

        resolved = resolve_review(spec_dir, cycle_id=c1.cycle_id, approved=True)
        assert resolved.state is CycleState.APPROVED
        assert resolved.is_resolved()
        assert len(resolved.resolutions) == 1
        assert resolved.resolutions[0]["outcome"] == "approved"

    def test_requested_to_started_to_changes_requested(self, spec_dir):
        c1 = request_review(spec_dir)
        record_started(spec_dir, cycle_id=c1.cycle_id)
        resolved = resolve_review(spec_dir, cycle_id=c1.cycle_id, approved=False)
        assert resolved.state is CycleState.CHANGES_REQUESTED
        assert resolved.resolutions[0]["outcome"] == "changes_requested"

    def test_state_persists_to_disk(self, spec_dir):
        c1 = request_review(spec_dir)
        record_started(spec_dir, cycle_id=c1.cycle_id)
        reloaded = load_cycle(spec_dir)
        assert reloaded is not None
        assert reloaded.state is CycleState.STARTED
        assert reloaded.cycle_id == c1.cycle_id

    def test_new_request_increments_cycle_id(self, spec_dir):
        c1 = request_review(spec_dir)
        record_started(spec_dir, cycle_id=c1.cycle_id)
        resolve_review(spec_dir, cycle_id=c1.cycle_id, approved=False)
        c2 = request_review(spec_dir)
        assert c2.cycle_id == 2
        assert c2.state is CycleState.REQUESTED
        # Prior resolution history is carried forward.
        assert len(c2.resolutions) == 1

    def test_record_started_is_idempotent_for_same_cycle(self, spec_dir):
        c1 = request_review(spec_dir)
        first = record_started(spec_dir, cycle_id=c1.cycle_id)
        first_proof_time = first.proof.recorded_at
        again = record_started(spec_dir, cycle_id=c1.cycle_id)
        # Same proof retained; no error, no second proof.
        assert again.proof.recorded_at == first_proof_time
        assert again.state is CycleState.STARTED


# =============================================================================
# STRICT-BOUNDARY GUARD — cross-cycle evidence must be rejected
# =============================================================================


class TestStrictBoundary:
    def test_old_cycle_evidence_rejected_against_new_request(self, spec_dir):
        # Cycle 1 fully completes (changes requested).
        c1 = request_review(spec_dir)
        record_started(spec_dir, cycle_id=c1.cycle_id)
        resolve_review(spec_dir, cycle_id=c1.cycle_id, approved=False)

        # A NEW review is requested (cycle 2).
        c2 = request_review(spec_dir)
        assert c2.cycle_id == 2

        # A stale reviewer reports "started" carrying the OLD cycle id. This is
        # the competitor's exact bug — it must be refused, not folded into c2.
        with pytest.raises(StaleCycleError):
            record_started(spec_dir, cycle_id=c1.cycle_id)

        # Cycle 2 remains untouched (still REQUESTED, no proof).
        current = load_cycle(spec_dir)
        assert current.cycle_id == 2
        assert current.state is CycleState.REQUESTED
        assert current.proof is None

    def test_stale_resolution_rejected(self, spec_dir):
        c1 = request_review(spec_dir)
        record_started(spec_dir, cycle_id=c1.cycle_id)
        request_review(spec_dir)  # opens cycle 2
        with pytest.raises(StaleCycleError):
            resolve_review(spec_dir, cycle_id=c1.cycle_id, approved=True)

    def test_proof_is_bound_to_its_cycle_id(self, spec_dir):
        c1 = request_review(spec_dir)
        started = record_started(spec_dir, cycle_id=c1.cycle_id)
        assert started.proof.cycle_id == 1
        # New cycle must not inherit cycle 1's proof.
        c2 = request_review(spec_dir)
        assert c2.proof is None


# =============================================================================
# LIFECYCLE ENFORCEMENT — no skipping the engagement step, no backwards moves
# =============================================================================


class TestLifecycleEnforcement:
    def test_cannot_resolve_unstarted_cycle(self, spec_dir):
        c1 = request_review(spec_dir)
        # No record_started: the review never provably happened.
        with pytest.raises(InvalidTransitionError):
            resolve_review(spec_dir, cycle_id=c1.cycle_id, approved=True)

    def test_record_started_without_cycle_errors(self, spec_dir):
        with pytest.raises(ReviewCycleError):
            record_started(spec_dir, cycle_id=1)

    def test_resolve_without_cycle_errors(self, spec_dir):
        with pytest.raises(ReviewCycleError):
            resolve_review(spec_dir, cycle_id=1, approved=True)


# =============================================================================
# EXACTLY-ONCE RESOLUTION
# =============================================================================


class TestExactlyOnceResolution:
    def test_double_resolve_rejected(self, spec_dir):
        c1 = request_review(spec_dir)
        record_started(spec_dir, cycle_id=c1.cycle_id)
        resolve_review(spec_dir, cycle_id=c1.cycle_id, approved=True)
        # Second resolution of the same terminal cycle must fail.
        with pytest.raises(InvalidTransitionError):
            resolve_review(spec_dir, cycle_id=c1.cycle_id, approved=True)
        # And only one resolution entry exists.
        assert len(load_cycle(spec_dir).resolutions) == 1

    def test_resolve_then_change_verdict_rejected(self, spec_dir):
        c1 = request_review(spec_dir)
        record_started(spec_dir, cycle_id=c1.cycle_id)
        resolve_review(spec_dir, cycle_id=c1.cycle_id, approved=False)
        with pytest.raises(InvalidTransitionError):
            resolve_review(spec_dir, cycle_id=c1.cycle_id, approved=True)


# =============================================================================
# UNTOUCHED-REVIEW DETECTION + RE-DRIVE
# =============================================================================


class TestUntouchedDetection:
    def test_fresh_request_not_yet_untouched(self, spec_dir):
        request_review(spec_dir)
        # With a positive timeout, a just-created request is not untouched.
        assert detect_untouched_review(spec_dir, timeout_seconds=300) is None

    def test_aged_unstarted_request_is_untouched(self, spec_dir):
        request_review(spec_dir)
        future = datetime.now(timezone.utc) + timedelta(seconds=301)
        stalled = detect_untouched_review(spec_dir, timeout_seconds=300, now=future)
        assert stalled is not None
        assert stalled.state is CycleState.REQUESTED

    def test_started_request_never_untouched(self, spec_dir):
        c1 = request_review(spec_dir)
        record_started(spec_dir, cycle_id=c1.cycle_id)
        far_future = datetime.now(timezone.utc) + timedelta(hours=1)
        assert (
            detect_untouched_review(spec_dir, timeout_seconds=1, now=far_future) is None
        )

    def test_no_cycle_returns_none(self, spec_dir):
        assert detect_untouched_review(spec_dir, timeout_seconds=0) is None

    def test_redrive_emits_signal_for_stalled_review(self, spec_dir):
        c1 = request_review(spec_dir)
        future = datetime.now(timezone.utc) + timedelta(seconds=301)
        signal = redrive_untouched_review(spec_dir, timeout_seconds=300, now=future)
        assert signal is not None
        assert signal["action"] == "redrive_review"
        assert signal["cycle_id"] == c1.cycle_id
        assert signal["state"] == CycleState.REQUESTED.value
        assert "never reached review_started" in signal["reason"]

    def test_redrive_silent_when_started(self, spec_dir):
        c1 = request_review(spec_dir)
        record_started(spec_dir, cycle_id=c1.cycle_id)
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        assert redrive_untouched_review(spec_dir, timeout_seconds=1, now=future) is None


# =============================================================================
# PERSISTENCE ROBUSTNESS
# =============================================================================


class TestPersistence:
    def test_load_missing_returns_none(self, spec_dir):
        assert load_cycle(spec_dir) is None

    def test_corrupt_file_returns_none(self, spec_dir):
        cycle_file_path(spec_dir).write_text("{ not json", encoding="utf-8")
        assert load_cycle(spec_dir) is None

    def test_roundtrip_to_from_dict(self, spec_dir):
        c1 = request_review(spec_dir)
        record_started(spec_dir, cycle_id=c1.cycle_id, detail={"k": "v"})
        resolve_review(spec_dir, cycle_id=c1.cycle_id, approved=True)
        loaded = load_cycle(spec_dir)
        assert loaded.proof.detail == {"k": "v"}
        assert loaded.state is CycleState.APPROVED


# =============================================================================
# RE-DRIVE STRIKE BOOKKEEPING (#260 delivery slice)
# =============================================================================


class TestRedriveBookkeeping:
    def test_record_redrive_increments_and_stamps(self, spec_dir):
        c1 = request_review(spec_dir)
        now = datetime.now(timezone.utc)
        updated = record_redrive(spec_dir, cycle_id=c1.cycle_id, at=now)
        assert updated.redrive_attempts == 1
        assert updated.last_redrive_at == now.isoformat()
        # Persisted to disk (single source of truth).
        assert load_cycle(spec_dir).redrive_attempts == 1

    def test_record_redrive_rejects_stale_cycle(self, spec_dir):
        c1 = request_review(spec_dir)
        request_review(spec_dir)  # supersedes c1 with cycle 2
        with pytest.raises(StaleCycleError):
            record_redrive(spec_dir, cycle_id=c1.cycle_id)

    def test_record_redrive_without_cycle_errors(self, spec_dir):
        with pytest.raises(ReviewCycleError):
            record_redrive(spec_dir, cycle_id=1)

    def test_window_elapsed_uses_request_time_before_first_strike(self, spec_dir):
        request_review(spec_dir)
        cycle = load_cycle(spec_dir)
        soon = datetime.now(timezone.utc) + timedelta(seconds=10)
        later = datetime.now(timezone.utc) + timedelta(seconds=301)
        assert cycle.redrive_window_elapsed(window_seconds=300, now=soon) is False
        assert cycle.redrive_window_elapsed(window_seconds=300, now=later) is True

    def test_window_elapsed_uses_last_redrive_after_strike(self, spec_dir):
        c1 = request_review(spec_dir)
        strike_at = datetime.now(timezone.utc) + timedelta(seconds=400)
        record_redrive(spec_dir, cycle_id=c1.cycle_id, at=strike_at)
        cycle = load_cycle(spec_dir)
        # Just after the strike: window not yet elapsed from last_redrive_at.
        assert (
            cycle.redrive_window_elapsed(
                window_seconds=300, now=strike_at + timedelta(seconds=10)
            )
            is False
        )
        # A full window after the strike: elapsed.
        assert (
            cycle.redrive_window_elapsed(
                window_seconds=300, now=strike_at + timedelta(seconds=301)
            )
            is True
        )
