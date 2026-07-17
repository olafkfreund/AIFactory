"""Tests for the RFC-0011 difficulty-tier classifier (``pfactory.tiers``).

Covers ``classify_tier`` (highest-wins precedence, scoped-label tolerance,
malformed input) and ``tier_for`` (default + migration/rewrite override), plus
the ``tier`` field wired into ``taxonomy.Classification``.
"""

from __future__ import annotations

from pfactory.taxonomy import classify_labels
from pfactory.tiers import Tier, classify_parallel, classify_tier, tier_for

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


# ── classify_parallel: opt-in / opt-out ────────────────────────────────────


def test_parallel_label_opts_in() -> None:
    assert classify_parallel(["factory:parallel"]) == (True, None)


def test_serial_label_opts_out() -> None:
    assert classify_parallel(["factory:serial"]) == (False, None)


def test_no_parallel_label_returns_none_for_caller_default() -> None:
    assert classify_parallel(["factory:hard", "bug"]) == (None, None)
    assert classify_parallel([]) == (None, None)


def test_serial_wins_over_parallel_regardless_of_order() -> None:
    # Explicit opt-out must beat opt-in so a deployment default is overridable.
    assert classify_parallel(["factory:parallel", "factory:serial"]) == (False, None)
    assert classify_parallel(["factory:serial", "factory:parallel"]) == (False, None)


def test_scoped_gitlab_label_forms_are_accepted() -> None:
    assert classify_parallel(["factory::parallel"]) == (True, None)
    assert classify_parallel(["factory::serial"]) == (False, None)
    assert classify_parallel(["FACTORY::Parallel"]) == (True, None)


def test_parallel_is_orthogonal_to_the_tier() -> None:
    # A tier label alone never implies parallelism, and vice versa.
    assert classify_parallel(["factory:low"]) == (None, None)
    assert classify_tier(["factory:parallel"]) is None


# ── classify_parallel: workers=N ───────────────────────────────────────────


def test_workers_label_parsed() -> None:
    assert classify_parallel(["factory:parallel", "factory:workers=4"]) == (True, 4)
    assert classify_parallel(["factory::workers=2"]) == (None, 2)


def test_workers_alone_does_not_enable_parallel() -> None:
    # workers=N only TUNES the cap; enabling stays an explicit opt-in.
    assert classify_parallel(["factory:workers=4"]) == (None, 4)


def test_malformed_workers_labels_are_ignored() -> None:
    for bad in (
        "factory:workers=0",
        "factory:workers=-2",
        "factory:workers=x",
        "factory:workers=1.5",
        "factory:workers=",
    ):
        assert classify_parallel(["factory:parallel", bad]) == (True, None)


def test_dict_shaped_and_malformed_labels_never_raise() -> None:
    assert classify_parallel([{"name": "factory:parallel"}]) == (True, None)
    assert classify_parallel(None) == (None, None)
    assert classify_parallel([None, 42, {"nope": 1}, "  "]) == (None, None)
