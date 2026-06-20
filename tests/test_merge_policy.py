"""Tests for the RFC-0011/RFC-0009 merge-gate decision (``merge.merge_policy``).

Exhaustive decision matrix incl. the refuse-below-floor and refuse-without-parity
cases for the low tier, tier-alias acceptance, and verdict/VAL coercion.
"""

from __future__ import annotations

import pytest
from merge.merge_policy import (
    AUTO_MERGE,
    HOLD_ASYNC,
    HOLD_BLOCKING,
    decide_merge,
)

GREEN = {
    "host_ci_green": True,
    "tfactory_verdict": "pass",
    "achieved_val": 2,
    "val_floor": 1,
    "ci_parity": True,
}


# ── tier dispatch ──────────────────────────────────────────────────────────


def test_hard_always_holds_blocking():
    assert decide_merge("hard", **GREEN) == HOLD_BLOCKING
    assert decide_merge("blocking", **GREEN) == HOLD_BLOCKING  # review_tier alias


def test_medium_always_holds_async():
    assert decide_merge("medium", **GREEN) == HOLD_ASYNC
    assert decide_merge("async", **GREEN) == HOLD_ASYNC  # review_tier alias


def test_unknown_tier_holds_blocking():
    assert decide_merge("weird", **GREEN) == HOLD_BLOCKING
    assert decide_merge("", **GREEN) == HOLD_BLOCKING


# ── low tier: the gated auto-merge ─────────────────────────────────────────


def test_low_auto_merges_when_all_green():
    assert decide_merge("low", **GREEN) == AUTO_MERGE
    assert decide_merge("auto", **GREEN) == AUTO_MERGE  # review_tier alias


def test_low_refuses_when_ci_not_green():
    assert decide_merge("low", **{**GREEN, "host_ci_green": False}) == HOLD_ASYNC


def test_low_refuses_when_verdict_not_pass():
    assert decide_merge("low", **{**GREEN, "tfactory_verdict": "fail"}) == HOLD_ASYNC
    assert decide_merge("low", **{**GREEN, "tfactory_verdict": None}) == HOLD_ASYNC
    assert decide_merge("low", **{**GREEN, "tfactory_verdict": "handback"}) == HOLD_ASYNC


def test_low_refuses_below_val_floor():
    # achieved VAL-1 but the floor is VAL-2 => must not auto-merge.
    out = decide_merge("low", **{**GREEN, "achieved_val": 1, "val_floor": 2})
    assert out == HOLD_ASYNC


def test_low_refuses_without_ci_parity():
    assert decide_merge("low", **{**GREEN, "ci_parity": False}) == HOLD_ASYNC


def test_low_auto_merges_when_val_exceeds_floor():
    assert decide_merge("low", **{**GREEN, "achieved_val": 3, "val_floor": 2}) == AUTO_MERGE


# ── coercion of verdict + VAL spellings ────────────────────────────────────


@pytest.mark.parametrize("verdict", [True, "pass", "passed", "GREEN", "ok"])
def test_verdict_pass_spellings(verdict):
    assert decide_merge("low", **{**GREEN, "tfactory_verdict": verdict}) == AUTO_MERGE


@pytest.mark.parametrize("verdict", [False, "fail", "failed", "", "handback", 0])
def test_verdict_fail_spellings(verdict):
    assert decide_merge("low", **{**GREEN, "tfactory_verdict": verdict}) == HOLD_ASYNC


@pytest.mark.parametrize(
    ("achieved", "floor", "expect"),
    [
        ("VAL-2", "VAL-1", AUTO_MERGE),
        ("val2", "val2", AUTO_MERGE),
        ("VAL-1", "VAL-3", HOLD_ASYNC),
        (2, 2, AUTO_MERGE),
        (None, 1, HOLD_ASYNC),  # missing achieved => fails the floor
    ],
)
def test_val_floor_spellings(achieved, floor, expect):
    out = decide_merge("low", **{**GREEN, "achieved_val": achieved, "val_floor": floor})
    assert out == expect


def test_no_floor_declared_allows_any_achieved():
    out = decide_merge("low", **{**GREEN, "achieved_val": 1, "val_floor": None})
    assert out == AUTO_MERGE


def test_tier_case_insensitive():
    assert decide_merge("LOW", **GREEN) == AUTO_MERGE
    assert decide_merge(" Hard ", **GREEN) == HOLD_BLOCKING
