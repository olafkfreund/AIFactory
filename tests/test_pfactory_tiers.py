"""Tests for the RFC-0011 difficulty-tier classifier (``pfactory.tiers``).

Covers ``classify_tier`` (highest-wins precedence, scoped-label tolerance,
malformed input) and ``tier_for`` (default + migration/rewrite override), plus
the ``tier`` field wired into ``taxonomy.Classification``.
"""

from __future__ import annotations

from pfactory.taxonomy import classify_labels
from pfactory.tiers import Tier, classify_tier, tier_for

# ── classify_tier: basic mapping ───────────────────────────────────────────


def test_single_tier_labels():
    assert classify_tier(["factory:low"]) is Tier.LOW
    assert classify_tier(["factory:medium"]) is Tier.MEDIUM
    assert classify_tier(["factory:hard"]) is Tier.HARD


def test_no_tier_label_returns_none():
    assert classify_tier(["pfactory", "handoff:aifactory"]) is None
    assert classify_tier([]) is None


# ── classify_tier: highest-wins precedence ─────────────────────────────────


def test_highest_wins_hard_over_medium_over_low():
    assert classify_tier(["factory:low", "factory:hard"]) is Tier.HARD
    assert classify_tier(["factory:low", "factory:medium"]) is Tier.MEDIUM
    assert classify_tier(["factory:medium", "factory:hard"]) is Tier.HARD
    assert classify_tier(["factory:low", "factory:medium", "factory:hard"]) is Tier.HARD


def test_precedence_independent_of_order():
    assert classify_tier(["factory:hard", "factory:low"]) is Tier.HARD


# ── classify_tier: tolerance ───────────────────────────────────────────────


def test_scoped_label_double_colon_tolerated():
    assert classify_tier(["factory::low"]) is Tier.LOW
    assert classify_tier(["factory::hard", "factory::medium"]) is Tier.HARD


def test_case_insensitive():
    assert classify_tier(["Factory:Hard"]) is Tier.HARD


def test_dict_labels_tolerated():
    assert classify_tier([{"name": "factory:medium"}]) is Tier.MEDIUM


def test_malformed_input_does_not_raise():
    assert classify_tier(None) is None
    assert classify_tier("factory:hard") is None  # bare string is "no labels"
    assert classify_tier([None, 42, {"no": "name"}]) is None
    assert classify_tier(["factory:bogus"]) is None


# ── tier_for: default + migration override ─────────────────────────────────


class _Stub:
    def __init__(self, tier):
        self.tier = tier


def test_tier_for_reads_classification_tier():
    assert tier_for(_Stub(Tier.LOW)) is Tier.LOW


def test_tier_for_defaults_to_medium_when_unset():
    assert tier_for(_Stub(None)) is Tier.MEDIUM
    assert tier_for(object()) is Tier.MEDIUM


def test_tier_for_migration_forces_hard():
    # Rewrite forces hard regardless of the labelled tier.
    assert tier_for(_Stub(Tier.LOW), change_mode="migration") is Tier.HARD
    assert tier_for(_Stub(Tier.MEDIUM), change_mode="migration") is Tier.HARD
    assert tier_for(_Stub(None), change_mode="MIGRATION") is Tier.HARD


def test_tier_for_non_migration_change_mode_keeps_tier():
    assert tier_for(_Stub(Tier.LOW), change_mode="feature") is Tier.LOW


# ── Classification.tier wiring ─────────────────────────────────────────────


def test_classification_carries_tier():
    c = classify_labels(["pfactory", "handoff:aifactory", "factory:hard"])
    assert c.tier is Tier.HARD


def test_classification_tier_none_when_unlabelled():
    c = classify_labels(["pfactory"])
    assert c.tier is None
