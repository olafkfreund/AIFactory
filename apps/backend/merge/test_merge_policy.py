"""Tests for the RFC-0011 merge-gate decision and the RFC-0013 deployment overlay.

The base ``decide_merge`` matrix (RFC-0011 / RFC-0009) and the additive
deployment overlay (#645 / RFC-0013): a high-risk or production change can never
auto-merge while its blocking system gates are unsatisfied, even at the lowest
tier. Absent deployment inputs must leave the base decision unchanged.
"""

from __future__ import annotations

import pytest
from merge.merge_policy import (
    AUTO_MERGE,
    HOLD_ASYNC,
    HOLD_BLOCKING,
    decide_merge,
    deployment_block_reasons,
)

# All gates satisfied for the happy-path low tier.
_GREEN = {
    "host_ci_green": True,
    "tfactory_verdict": "pass",
    "achieved_val": "VAL-3",
    "val_floor": "VAL-2",
    "ci_parity": True,
}


# --------------------------------------------------------------------------- #
# Base RFC-0011 matrix (back-compat — no deployment block).
# --------------------------------------------------------------------------- #


def test_low_tier_all_green_auto_merges() -> None:
    assert decide_merge("low", **_GREEN) == AUTO_MERGE
    assert decide_merge("auto", **_GREEN) == AUTO_MERGE


@pytest.mark.parametrize(
    "override",
    [
        {"host_ci_green": False},
        {"tfactory_verdict": "fail"},
        {"tfactory_verdict": None},
        {"achieved_val": "VAL-1"},
        {"ci_parity": False},
    ],
)
def test_low_tier_any_failing_gate_degrades_to_async(
    override: dict[str, object],
) -> None:
    kwargs = {**_GREEN, **override}
    assert decide_merge("low", **kwargs) == HOLD_ASYNC


def test_medium_tier_holds_async() -> None:
    assert decide_merge("medium", **_GREEN) == HOLD_ASYNC
    assert decide_merge("async", **_GREEN) == HOLD_ASYNC


def test_hard_tier_holds_blocking() -> None:
    assert decide_merge("hard", **_GREEN) == HOLD_BLOCKING
    assert decide_merge("blocking", **_GREEN) == HOLD_BLOCKING


def test_unknown_tier_holds_blocking() -> None:
    assert decide_merge("nonsense", **_GREEN) == HOLD_BLOCKING


def test_val_floor_absent_any_achieved_satisfies() -> None:
    kwargs = {**_GREEN, "val_floor": None, "achieved_val": "VAL-1"}
    assert decide_merge("low", **kwargs) == AUTO_MERGE


# --------------------------------------------------------------------------- #
# RFC-0013 deployment overlay — never loosen, only tighten.
# --------------------------------------------------------------------------- #


def test_absent_deployment_block_unchanged() -> None:
    assert decide_merge("low", **_GREEN, deployment=None) == AUTO_MERGE
    assert decide_merge("low", **_GREEN, deployment={}) == AUTO_MERGE


def test_low_risk_deployment_still_auto_merges() -> None:
    deployment = {
        "risk_class": "low",
        "production_classification": "internal",
        "system_gates": ["ci-green"],
    }
    assert decide_merge("low", **_GREEN, deployment=deployment) == AUTO_MERGE


def test_high_risk_blocks_auto_merge_at_low_tier() -> None:
    deployment = {"risk_class": "high", "system_gates": ["ci-green"]}
    assert decide_merge("low", **_GREEN, deployment=deployment) == HOLD_BLOCKING


def test_production_blocks_auto_merge_at_low_tier() -> None:
    deployment = {
        "risk_class": "medium",
        "production_classification": "production",
        "system_gates": ["ci-green", "human-approval"],
    }
    assert decide_merge("low", **_GREEN, deployment=deployment) == HOLD_BLOCKING


def test_unsatisfied_human_approval_gate_blocks() -> None:
    deployment = {
        "risk_class": "medium",
        "system_gates": ["ci-green", "human-approval"],
    }
    assert decide_merge("low", **_GREEN, deployment=deployment) == HOLD_BLOCKING


def test_satisfied_human_approval_gate_does_not_block_on_gate_alone() -> None:
    # Medium risk + the only blocking gate cleared => deployment imposes no block,
    # so the base low-tier decision (auto-merge) stands.
    deployment = {
        "risk_class": "medium",
        "system_gates": ["ci-green", "human-approval"],
    }
    decision = decide_merge(
        "low", **_GREEN, deployment=deployment, satisfied_gates=["human-approval"]
    )
    assert decision == AUTO_MERGE


def test_production_blocks_even_with_human_approval_satisfied() -> None:
    # Production classification is never autonomous regardless of gate state.
    deployment = {
        "production_classification": "production",
        "system_gates": ["human-approval"],
    }
    decision = decide_merge(
        "low", **_GREEN, deployment=deployment, satisfied_gates=["human-approval"]
    )
    assert decision == HOLD_BLOCKING


def test_unknown_dora_health_never_relaxes() -> None:
    # available=false delivery health is UNKNOWN, not healthy: it must not turn a
    # blocked change into an auto-merge.
    deployment = {
        "risk_class": "high",
        "dora_context": {"available": False, "reason": "no provider"},
    }
    assert decide_merge("low", **_GREEN, deployment=deployment) == HOLD_BLOCKING


# --------------------------------------------------------------------------- #
# deployment_block_reasons — the auditable reason list.
# --------------------------------------------------------------------------- #


def test_reasons_empty_for_none_and_low_risk() -> None:
    assert deployment_block_reasons(None) == []
    assert deployment_block_reasons({"risk_class": "low"}) == []


def test_reasons_lists_high_risk_and_production_and_gate() -> None:
    deployment = {
        "risk_class": "high",
        "production_classification": "production",
        "system_gates": ["human-approval"],
    }
    reasons = deployment_block_reasons(deployment)
    joined = " ".join(reasons)
    assert "risk_class=high" in joined
    assert "production" in joined
    assert "human-approval" in joined
    assert len(reasons) == 3


def test_reasons_malformed_block_never_raises() -> None:
    assert deployment_block_reasons({"system_gates": "not-a-list"}) == []
    assert deployment_block_reasons({"risk_class": 123}) == []


def test_reasons_satisfied_gate_drops_from_list() -> None:
    deployment = {"system_gates": ["human-approval"]}
    assert deployment_block_reasons(deployment) == [
        "required system gate 'human-approval' is not satisfied"
    ]
    assert (
        deployment_block_reasons(deployment, satisfied_gates=["human-approval"]) == []
    )
