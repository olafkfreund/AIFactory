"""Fail-open holes in the RFC-0011 merge-decision matrix (``merge.merge_policy``).

Every guard below replaces a branch that returned the PERMISSIVE answer when the
gate could not be evaluated. The pattern is the same each time and it is the
dangerous one: the single failure mode that removes a check also removes the
check's ability to say no, so the gate reads as present and passes everything.

These assert through ``decide_merge`` rather than the private helpers wherever
the helper is reachable that way -- a test on a private ranking function passes
with the decision still wired to the old behaviour.
"""

from __future__ import annotations

import sys

import pytest
from merge.merge_policy import (
    AUTO_MERGE,
    HOLD_ASYNC,
    HOLD_BLOCKING,
    decide_merge,
    deployment_block_reasons,
    floor_from_paths,
    raise_review_tier,
)

_GREEN = {
    "host_ci_green": True,
    "tfactory_verdict": "pass",
    "achieved_val": 3,
    "val_floor": 1,
    "ci_parity": True,
}


# --------------------------------------------------------------------------- #
# floor_from_paths: a classifier that did not run has cleared nothing
# --------------------------------------------------------------------------- #


def test_floor_fails_closed_when_the_risk_table_cannot_be_imported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The auth/secrets/migrations/infra/CI classifier used to return "auto" on
    ImportError. "auto" is bucket rank 0, NOT the -1 raise_review_tier gives an
    unrecognised value, so the vanished classifier could not raise a single
    tier -- the floor silently disappeared for every change."""
    monkeypatch.setitem(sys.modules, "review_tier", None)
    assert floor_from_paths(["app/auth/session.py"]) == "blocking"
    assert floor_from_paths(["README.md"]) == "blocking"
    assert floor_from_paths([]) == "blocking"


def test_the_closed_floor_can_actually_raise_a_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The point of "blocking" over "auto": it outranks every real tier."""
    monkeypatch.setitem(sys.modules, "review_tier", None)
    floor = floor_from_paths(["README.md"])
    assert raise_review_tier("auto", floor) == "blocking"
    assert raise_review_tier("async", floor) == "blocking"


def test_a_readable_classifier_still_reports_no_floor_for_a_safe_diff() -> None:
    """Fail-closed on ImportError must not become always-closed."""
    assert floor_from_paths(["README.md"]) == "auto"


# --------------------------------------------------------------------------- #
# _val_meets_floor: an unreadable floor is not an absent floor
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("floor", ["VAL-three", "high", "  ?  ", "none"])
def test_an_unparseable_val_floor_is_not_a_waived_val_floor(floor: str) -> None:
    """It used to return True, silently disabling the VAL gate for any floor
    spelled in words -- while the sibling branch two lines up already failed
    closed on a missing achieved level."""
    assert decide_merge("low", **{**_GREEN, "val_floor": floor}) == HOLD_ASYNC


@pytest.mark.parametrize("floor", [None, "", "   "])
def test_an_absent_val_floor_still_declares_no_floor(floor: object) -> None:
    """Back-compat: no floor declared => any achieved level satisfies it."""
    assert decide_merge("low", **{**_GREEN, "val_floor": floor}) == AUTO_MERGE


# --------------------------------------------------------------------------- #
# _val_rank: the FIRST integer run, never every digit concatenated
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("achieved", "floor"),
    [
        ("VAL-1 of 3", 2),  # joined digits -> 13, outranks every floor
        ("VAL-2 (3 lanes)", 3),  # joined digits -> 23
        ("VAL-1, 2 lanes skipped", 4),  # joined digits -> 12
    ],
)
def test_an_annotated_val_cannot_outrank_the_floor(achieved: str, floor: int) -> None:
    assert (
        decide_merge("low", **{**_GREEN, "achieved_val": achieved, "val_floor": floor})
        == HOLD_ASYNC
    )


@pytest.mark.parametrize(
    ("achieved", "floor"), [("VAL-3", 2), ("val3", "VAL-2"), ("3", 3)]
)
def test_a_plain_val_still_ranks_normally(achieved: str, floor: object) -> None:
    assert (
        decide_merge("low", **{**_GREEN, "achieved_val": achieved, "val_floor": floor})
        == AUTO_MERGE
    )


# --------------------------------------------------------------------------- #
# deployment_block_reasons: the docstring's promise, actually kept
# --------------------------------------------------------------------------- #


def test_pre_deploy_scans_hold_the_merge_like_the_docstring_says() -> None:
    """It intersected the required gates with {"human-approval"}, so a contract
    declaring security-scan / sbom / dr-signoff produced ZERO reasons."""
    deployment = {"system_gates": ["security-scan", "sbom", "dr-signoff"]}
    reasons = deployment_block_reasons(deployment)
    assert len(reasons) == 3
    assert decide_merge("low", **_GREEN, deployment=deployment) == HOLD_BLOCKING


def test_a_gate_is_satisfiable_so_the_hold_can_be_lifted() -> None:
    deployment = {"system_gates": ["security-scan", "human-approval"]}
    cleared = ["security-scan", "human-approval"]
    assert deployment_block_reasons(deployment, satisfied_gates=cleared) == []
    assert (
        decide_merge("low", **_GREEN, deployment=deployment, satisfied_gates=cleared)
        == AUTO_MERGE
    )


def test_the_human_only_gate_says_so_in_the_reason() -> None:
    """The audit trail must distinguish a gate CI can clear from one it cannot."""
    human = deployment_block_reasons({"system_gates": ["human-approval"]})
    machine = deployment_block_reasons({"system_gates": ["sbom"]})
    assert "only a human can clear it" in human[0]
    assert "only a human" not in machine[0]
