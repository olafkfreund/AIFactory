#!/usr/bin/env python3
"""
Calibration tests for the BMad structural-floor complexity signal (issue #504).
================================================================================

The structural floor in ``ComplexityDetector`` raises the detected level when a
task description (or its requirements) shows clear multi-endpoint / multi-layer
breadth — even when the prose matched a low-level keyword like "add".

Because changing complexity thresholds reroutes EVERY task, these tests act as a
calibration set with two guarantees:

1. **Under-classification is fixed** — the documented multi-layer features
   (issue #504, task 009) now classify at least Standard (Level 2 / Standard
   track), so the BMad story-planner engages.
2. **No regression** — a battery of genuinely trivial/simple tasks must stay in
   the Quick Flow (Level <= 1). The floor is additive; it must never push small
   tasks into the expensive Standard/Enterprise pipelines.
"""


import pytest

# conftest.py inserts apps/backend on sys.path and mocks the SDK modules.
from integrations.bmad.complexity_detector import ComplexityDetector, Track


@pytest.fixture
def detector():
    return ComplexityDetector()


# ---------------------------------------------------------------------------
# Cases that MUST classify at least Standard (the bug the issue reports)
# ---------------------------------------------------------------------------

# The verbatim task-009 description from issue #504.
TASK_009 = (
    "add GET /health + POST /echo endpoints with Pydantic request/response "
    "models, wire routes into the app, pytest unit tests covering success + "
    "validation errors — multi-file feature touching models, routes, app "
    "wiring, and tests."
)

SHOULD_BE_AT_LEAST_STANDARD = [
    TASK_009,
    # Two endpoints alone is enough breadth.
    "add GET /users and POST /users endpoints",
    # Single endpoint but three distinct layers (model + route + test).
    "add a GET /orders endpoint with a pydantic model and a pytest test",
    # Four layers, no literal endpoint verbs.
    "implement the order model, the route handler, wire it into the app, and "
    "add unit tests",
]


@pytest.mark.parametrize("description", SHOULD_BE_AT_LEAST_STANDARD)
def test_multi_layer_features_reach_standard(detector, description):
    """Multi-endpoint / multi-layer features classify at >= Level 2 (Standard)."""
    result = detector.detect(description)
    assert result.level >= 2, (
        f"expected >= Level 2 (Standard) for multi-layer feature, "
        f"got Level {result.level}: {result.reasoning!r}"
    )
    assert result.track in (Track.STANDARD, Track.ENTERPRISE), (
        f"expected Standard/Enterprise track, got {result.track}"
    )


def test_task_009_specifically_engages_story_planner(detector):
    """The exact regression from issue #504 no longer routes to Quick Flow."""
    result = detector.detect(TASK_009)
    assert result.level >= 2
    assert result.track is not Track.QUICK_FLOW


# ---------------------------------------------------------------------------
# Cases that MUST NOT regress upward (blast-radius guard)
# ---------------------------------------------------------------------------

SHOULD_STAY_QUICK_FLOW = [
    "Fix typo in the README",
    "change the submit button color to blue",
    "update the footer copyright year",
    "rename the getUserData function to fetchUser",
    "add a loading spinner to the login form",
    "hide the debug banner in production",
    # A single endpoint is genuinely small — must not trip the floor.
    "add a single GET /ping endpoint that returns ok",
    # Two layers (test + service) is below the floor threshold.
    "add a unit test for the user service",
]


@pytest.mark.parametrize("description", SHOULD_STAY_QUICK_FLOW)
def test_trivial_tasks_stay_quick_flow(detector, description):
    """Trivial/simple tasks remain Level <= 1 (Quick Flow) — no over-classification."""
    result = detector.detect(description)
    assert result.level <= 1, (
        f"trivial task over-classified to Level {result.level}: {result.reasoning!r}"
    )
    assert result.track is Track.QUICK_FLOW


# ---------------------------------------------------------------------------
# Requirements-based floor (acceptance criteria / services count)
# ---------------------------------------------------------------------------


def test_many_acceptance_criteria_raise_to_standard(detector):
    """A short 'add' task with many distinct deliverables classifies Standard."""
    requirements = {
        "acceptance_criteria": [
            "endpoint returns 200 on success",
            "endpoint returns 422 on invalid body",
            "response matches the pydantic schema",
            "request is persisted to the database",
            "unit tests cover success and validation",
        ]
    }
    result = detector.detect("add the echo feature", requirements=requirements)
    assert result.level >= 2


def test_multiple_services_raise_to_complex(detector):
    """Work spanning 3+ services classifies Complex (Level 3)."""
    requirements = {"services_involved": ["backend", "frontend", "worker"]}
    result = detector.detect("add the notifications feature", requirements=requirements)
    assert result.level >= 3


def test_requirements_none_is_safe(detector):
    """Passing no requirements must behave exactly like the description-only path."""
    a = detector.detect("Fix typo in the README")
    b = detector.detect("Fix typo in the README", requirements=None)
    assert a.level == b.level == 0


def test_floor_never_lowers_a_high_keyword_level(detector):
    """The floor is additive — a clearly-enterprise task is never lowered."""
    result = detector.detect(
        "build a multi-tenant enterprise platform with microservice architecture"
    )
    assert result.level >= 3
