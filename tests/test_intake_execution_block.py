"""Tests for ``intake.execution_block.build_execution_block`` (RFC-0011 #635).

Pure tier -> execution-block mapping; the low-tier Ollama probe is injected so
no network is touched.
"""

from __future__ import annotations

import pytest
from intake.execution_block import build_execution_block
from pfactory.tiers import Tier


def test_low_tier_uses_injected_resolver():
    block = build_execution_block(Tier.LOW, low_model_resolver=lambda: "ollama:qwen3")
    assert block["model"] == "ollama:qwen3"
    assert block["skip_planning"] is True
    assert block["review_tier"] == "auto"
    assert block["complexity"] == "simple"
    assert block["autonomy_tier"] == "low"


def test_medium_tier():
    block = build_execution_block(Tier.MEDIUM)
    assert block["model"] == "sonnet"
    assert block["skip_planning"] is True
    assert block["review_tier"] == "async"
    assert block["complexity"] == "standard"
    assert block["autonomy_tier"] == "medium"


def test_hard_tier_full_planning():
    block = build_execution_block(Tier.HARD)
    assert block["model"] == "opus"
    assert block["skip_planning"] is False  # hard runs full decompose
    assert block["review_tier"] == "blocking"
    assert block["complexity"] == "complex"
    assert block["autonomy_tier"] == "hard"


def test_change_mode_carried_through():
    block = build_execution_block(Tier.HARD, change_mode="migration")
    assert block["change_mode"] == "migration"


def test_change_mode_absent_when_not_given():
    assert "change_mode" not in build_execution_block(Tier.MEDIUM)


def test_low_tier_default_resolver_probes_ollama(monkeypatch):
    # Without an injected resolver it falls back to resolve_low_tier_model;
    # patch that to avoid the network.
    import intake.execution_block as eb

    monkeypatch.setattr(eb, "resolve_low_tier_model", lambda: "haiku")
    assert build_execution_block(Tier.LOW)["model"] == "haiku"


def test_rejects_non_tier():
    with pytest.raises(TypeError):
        build_execution_block("low")  # type: ignore[arg-type]
