"""
Tests for Issue #287 — a totally-failed build must not be masked as
"completed".

Symptom: a build whose subprocess exited cleanly (return_code == 0) but where
NO subtask completed and at least one failed/stuck still ended in the terminal
``human_review`` status with reviewReason ``"completed"`` (empty diff, "0 done /
N failed"). It should instead land in a failure state (``human_review`` +
reviewReason ``"errors"`` — the established needs-attention reason), via the
FAILED phase.

These tests pin the decision down to the pure ``is_failed_build`` predicate plus
the phase→status / phase→reviewReason mappings that _monitor_process consumes:

(a) 0 done / N failed + no commits  → treated as a failed build (NOT completed)
(b) all completed                   → unchanged success
(c) partial (some done, some failed)→ unchanged review behaviour (NOT failed)
"""

from __future__ import annotations

import sys
from pathlib import Path

# Add web-server source root to path so we can import the service module
_WEB_SERVER = Path(__file__).parent.parent / "apps" / "web-server"
sys.path.insert(0, str(_WEB_SERVER))

from server.services.agent_service import (  # noqa: E402
    TaskPhase,
    is_failed_build,
    phase_to_review_reason,
    phase_to_status,
)


def _plan(*statuses: str) -> dict:
    """Build a minimal implementation_plan.json dict with the given subtask
    statuses spread across one phase."""
    return {
        "phases": [
            {
                "subtasks": [
                    {"id": f"1.{i}", "status": s}
                    for i, s in enumerate(statuses, start=1)
                ]
            }
        ]
    }


class TestIsFailedBuild:
    """Pure predicate: did a finished build actually fail to make progress?"""

    # (a) 0 done / N failed + no commits → failure
    def test_all_failed_no_progress_is_failed(self):
        assert is_failed_build(_plan("failed", "failed", "failed")) is True

    def test_all_stuck_is_failed(self):
        assert is_failed_build(_plan("stuck", "stuck")) is True

    def test_failed_and_pending_no_completed_is_failed(self):
        # Some never ran (pending), the rest failed, none completed.
        assert is_failed_build(_plan("failed", "pending", "pending")) is True

    # (b) all completed → success (NOT a failed build)
    def test_all_completed_is_not_failed(self):
        assert is_failed_build(_plan("completed", "completed")) is False

    # (c) partial (some done, some failed) → genuine partial review, NOT failed
    def test_partial_some_done_some_failed_is_not_failed(self):
        assert is_failed_build(_plan("completed", "failed")) is False

    def test_one_completed_rest_stuck_is_not_failed(self):
        assert is_failed_build(_plan("completed", "stuck", "stuck")) is False

    # Guards: don't flip non-failure / ambiguous shapes to failed.
    def test_empty_plan_is_not_failed(self):
        assert is_failed_build({"phases": []}) is False
        assert is_failed_build({}) is False

    def test_all_pending_nothing_ran_is_not_failed(self):
        # No completions but also no failures — not a "failed build" signal.
        assert is_failed_build(_plan("pending", "pending")) is False

    def test_subtasks_across_multiple_phases(self):
        plan = {
            "phases": [
                {"subtasks": [{"id": "1.1", "status": "failed"}]},
                {"subtasks": [{"id": "2.1", "status": "stuck"}]},
            ]
        }
        assert is_failed_build(plan) is True


class TestTerminalStatusMapping:
    """The phase the predicate selects must map to the right kanban state.

    _monitor_process picks COMPLETED when build_succeeded, FAILED otherwise.
    """

    def test_failed_build_maps_to_human_review_errors(self):
        # (a) A failed build => FAILED phase => human_review + "errors".
        assert phase_to_status(TaskPhase.FAILED) == "human_review"
        assert phase_to_review_reason(TaskPhase.FAILED) == "errors"
        # And critically NOT the success reviewReason.
        assert phase_to_review_reason(TaskPhase.FAILED) != "completed"

    def test_successful_build_maps_to_human_review_completed(self):
        # (b) Genuine success keeps the COMPLETED → "completed" path.
        assert phase_to_status(TaskPhase.COMPLETED) == "human_review"
        assert phase_to_review_reason(TaskPhase.COMPLETED) == "completed"

    def test_all_failed_plan_does_not_select_completed_phase(self):
        # End-to-end of the decision the monitor makes for case (a):
        plan = _plan("failed", "failed")
        build_succeeded = not is_failed_build(plan)  # mirrors _monitor_process
        phase = TaskPhase.COMPLETED if build_succeeded else TaskPhase.FAILED
        assert phase is TaskPhase.FAILED
        assert phase_to_review_reason(phase) == "errors"

    def test_all_completed_plan_selects_completed_phase(self):
        # End-to-end for case (b):
        plan = _plan("completed", "completed")
        build_succeeded = not is_failed_build(plan)
        phase = TaskPhase.COMPLETED if build_succeeded else TaskPhase.FAILED
        assert phase is TaskPhase.COMPLETED
        assert phase_to_review_reason(phase) == "completed"

    def test_partial_plan_selects_completed_phase_unchanged(self):
        # End-to-end for case (c): partial review behaviour is untouched —
        # build_succeeded stays True so the existing review path runs.
        plan = _plan("completed", "failed")
        build_succeeded = not is_failed_build(plan)
        assert build_succeeded is True
        phase = TaskPhase.COMPLETED if build_succeeded else TaskPhase.FAILED
        assert phase is TaskPhase.COMPLETED
