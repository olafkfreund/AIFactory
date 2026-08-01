"""Tests for phase_to_status's fallback on an unmapped TaskPhase (Factory#431).

Every current ``TaskPhase`` member has an entry in the mapping, so the
fallback in ``phase_to_status`` only ever fires for a phase the table has
never heard of -- a future enum member added without updating the table, or a
caller that bypassed the enum with a raw string. That used to default to
"in_progress", asserting the task was actively moving when nobody decided
that; a task in a phase nobody planned for would silently sit in the "still
working" kanban column forever instead of surfacing for a human to look at.
"""

from __future__ import annotations

import logging
from typing import cast

from server.services.task_phase import TaskPhase, phase_to_status


def test_every_known_phase_maps_as_before() -> None:
    assert phase_to_status(TaskPhase.SPEC_CREATION) == "in_progress"
    assert phase_to_status(TaskPhase.PLANNING) == "in_progress"
    assert phase_to_status(TaskPhase.PLAN_REVIEW) == "human_review"
    assert phase_to_status(TaskPhase.CODING) == "in_progress"
    assert phase_to_status(TaskPhase.QA_REVIEW) == "ai_review"
    assert phase_to_status(TaskPhase.QA_FIXING) == "in_progress"
    assert phase_to_status(TaskPhase.COMPLETED) == "human_review"
    assert phase_to_status(TaskPhase.FAILED) == "human_review"


def test_unmapped_phase_routes_to_human_review_not_in_progress(caplog) -> None:
    """An unmapped phase must not be reported as "in_progress" -- that
    invents progress nobody confirmed. It should route to the same
    already-accepted "human_review" status COMPLETED/FAILED use, and log a
    warning naming the phase, rather than silently claiming the task is
    still moving.
    """
    # No real TaskPhase member is unmapped today; simulate the future-member
    # case the same way the enum's own str-equality would see it.
    unmapped_phase = cast("TaskPhase", "some_future_phase")
    with caplog.at_level(logging.WARNING):
        status = phase_to_status(unmapped_phase)
    assert status == "human_review"
    assert status != "in_progress"
    assert "some_future_phase" in caplog.text
