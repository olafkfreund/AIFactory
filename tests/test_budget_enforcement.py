"""Tests for RFC-0014 §7 budget enforcement (#664).

Covers ``budget_mode`` parsing, the observe-only default (never blocks — today's
behaviour), enforce-mode within-budget pass-through, downgrade-within-floor,
fail-fast with a NAMED reason, the governed no-silent-degrade rule, and the
subscription/local "no $ estimate cannot block" billing-mode parity.
"""

from __future__ import annotations

import core.budget_enforcement as be
import core.cost_router_core as crc
import pytest


@pytest.fixture
def catalog() -> dict[str, crc.ModelEntry]:
    loaded: dict[str, crc.ModelEntry] = crc.load_catalog(be.DEFAULT_CATALOG_PATH)
    return loaded


@pytest.fixture
def metered_catalog(catalog: dict[str, crc.ModelEntry]) -> dict[str, crc.ModelEntry]:
    # Metered-only sub-catalog: forces the downgrade to land on a metered model
    # (subscription/local would otherwise win as cost 0.0).
    return {
        mid: entry
        for mid, entry in catalog.items()
        if (entry.get("price") or {}).get("mode") == "metered"
    }


def test_parse_budget_mode_defaults_observe() -> None:
    assert be.parse_budget_mode(None) == "observe"
    assert be.parse_budget_mode({}) == "observe"
    assert be.parse_budget_mode({"budget_mode": "enforce"}) == "enforce"
    assert be.parse_budget_mode({"budget_mode": "nonsense"}) == "observe"


def test_cost_ceiling_resolution() -> None:
    assert be.cost_ceiling_usd({"routing": {"cost_ceiling_usd": 5.0}}) == 5.0
    # Legacy flat budget_usd is honoured when routing has no ceiling.
    assert be.cost_ceiling_usd({"budget_usd": 3.0}) == 3.0
    # routing ceiling wins over the legacy flat one.
    assert (
        be.cost_ceiling_usd({"budget_usd": 3.0, "routing": {"cost_ceiling_usd": 9.0}})
        == 9.0
    )
    assert be.cost_ceiling_usd({}) is None


def test_observe_never_blocks(catalog: dict[str, crc.ModelEntry]) -> None:
    execution = {
        "phase_models": {"planning": "opus", "coding": "opus", "qa": "opus"},
        "routing": {"cost_ceiling_usd": 0.01},
    }
    decision = be.enforce_budget(execution, catalog=catalog)
    assert decision.proceed is True
    assert decision.outcome == "observe"
    assert decision.estimate_usd is not None
    assert decision.estimate_usd > 0.01  # estimate computed, but not enforced


def test_enforce_within_budget(catalog: dict[str, crc.ModelEntry]) -> None:
    execution = {
        "budget_mode": "enforce",
        "phase_models": {"coding": "haiku"},
        "routing": {"cost_ceiling_usd": 100.0, "class": "economy", "difficulty": "low"},
    }
    decision = be.enforce_budget(execution, catalog=catalog)
    assert decision.proceed is True
    assert decision.outcome == "within_budget"


def test_enforce_downgrades_within_floor(
    metered_catalog: dict[str, crc.ModelEntry],
) -> None:
    # medium tier (balanced floor): opus everywhere over a small ceiling must be
    # re-picked to the cheapest balanced-floor metered model (NOT opus, and never
    # below the balanced floor — so not the cheap-class haiku).
    execution = {
        "budget_mode": "enforce",
        "phase_models": {"coding": "opus", "qa": "opus"},
        "routing": {
            "cost_ceiling_usd": 5.0,
            "class": "standard",
            "difficulty": "medium",
        },
    }
    decision = be.enforce_budget(execution, catalog=metered_catalog)
    assert decision.outcome == "downgraded"
    assert decision.proceed is True
    assert decision.phase_models["coding"] != "claude-opus-4-8"  # downgraded off opus
    assert (
        decision.phase_models["coding"] != "claude-haiku-4-5-20251001"
    )  # not below floor
    picked = metered_catalog[decision.phase_models["coding"]]
    assert picked["class"] in {"balanced", "frontier"}  # at/above the balanced floor
    assert decision.estimate_usd is not None
    assert decision.estimate_usd <= 5.0


def test_governed_fails_fast_never_degrades(catalog: dict[str, crc.ModelEntry]) -> None:
    execution = {
        "budget_mode": "enforce",
        "phase_models": {"planning": "opus", "coding": "opus"},
        "routing": {
            "cost_ceiling_usd": 0.01,
            "class": "governed",
            "difficulty": "hard",
        },
    }
    decision = be.enforce_budget(execution, catalog=catalog)
    assert decision.proceed is False
    assert decision.outcome == "fail_fast"
    assert "governed" in decision.reason
    # Models are preserved verbatim — never silently degraded.
    assert decision.phase_models["planning"] == "opus"


def test_floor_too_expensive_fails_fast(
    metered_catalog: dict[str, crc.ModelEntry],
) -> None:
    # hard tier forces a frontier floor; cheapest frontier is opus, so a tiny
    # ceiling cannot be met even after downgrade -> fail fast (and NOT governed).
    execution = {
        "budget_mode": "enforce",
        "phase_models": {"coding": "opus"},
        "routing": {
            "cost_ceiling_usd": 0.01,
            "class": "standard",
            "difficulty": "hard",
        },
    }
    decision = be.enforce_budget(execution, catalog=metered_catalog)
    assert decision.proceed is False
    assert decision.outcome == "fail_fast"
    assert "floor" in decision.reason


def test_subscription_local_cannot_block(catalog: dict[str, crc.ModelEntry]) -> None:
    execution = {
        "budget_mode": "enforce",
        "phase_models": {"coding": "ollama:qwen", "qa": "codex"},
        "routing": {"cost_ceiling_usd": 0.01, "difficulty": "low"},
    }
    decision = be.enforce_budget(execution, catalog=catalog)
    assert decision.proceed is True
    assert decision.estimate_usd is None


def test_estimate_build_cost_mixes_metered_and_flat(
    catalog: dict[str, crc.ModelEntry],
) -> None:
    execution = {"phase_models": {"coding": "haiku", "qa": "codex"}}
    total, per_role = be.estimate_build_cost(execution, catalog)
    # haiku is metered (a $ figure); codex is subscription (None).
    assert per_role["coding"] is not None
    assert per_role["qa"] is None
    assert total == per_role["coding"]


def test_default_catalog_loads() -> None:
    cat = crc.load_catalog(be.DEFAULT_CATALOG_PATH)
    assert "claude-opus-4-8" in cat


def test_module_self_tests_pass() -> None:
    be._test()
