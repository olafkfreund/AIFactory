#!/usr/bin/env python3
"""Tests for per-category token attribution (#262).

Covers:
- category attribution correctness (known segments -> expected split)
- exact reconciliation of per-category integers to the real SDK total
- cache-token -> system_instructions attribution
- %-of-window math
- atomic aggregation persistence across turns
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).parent.parent / "apps" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from agents.token_attribution import (  # noqa: E402
    CHARS_PER_TOKEN,
    PromptSegments,
    TurnUsage,
    attribute_turn,
    context_window_for_model,
    estimate_tokens,
    read_breakdown,
    record_turn,
    render_breakdown,
    usage_file_path,
)


def _cat(breakdown: dict, key: str) -> dict:
    return next(c for c in breakdown["categories"] if c["key"] == key)


def test_estimate_tokens_uses_char_heuristic():
    text = "x" * 400
    assert estimate_tokens(text) == round(400 / CHARS_PER_TOKEN)
    assert estimate_tokens("") == 0


def test_cached_tokens_attributed_to_system_instructions():
    """Cache read/creation tokens land wholly on system_instructions."""
    segments = PromptSegments(user_prompt="u" * 400)
    usage = TurnUsage(
        input_tokens=100,
        output_tokens=0,
        cache_read_tokens=5000,
        cache_creation_tokens=1000,
    )
    attr = attribute_turn(segments, usage)
    assert attr.categories["system_instructions"].tokens == 6000
    # The 100 fresh input tokens go to user_messages (only fresh segment).
    assert attr.categories["user_messages"].tokens == 100


def test_fresh_input_split_proportional_to_segments():
    """Fresh (non-cached) input splits by estimated segment weight."""
    # user weight 100 tok, coordination 100 tok, tool 200 tok -> 1:1:2
    segments = PromptSegments(
        user_prompt="u" * 400,            # ~100 tok
        coordination_context="c" * 400,   # ~100 tok
        tool_output_chars=800,            # ~200 tok
    )
    usage = TurnUsage(
        input_tokens=400,
        cache_read_tokens=1000,  # cache present -> system funded separately
    )
    attr = attribute_turn(segments, usage)
    assert attr.categories["system_instructions"].tokens == 1000
    # 400 fresh split 1:1:2 -> 100 / 100 / 200
    assert attr.categories["user_messages"].tokens == 100
    assert attr.categories["coordination_context"].tokens == 100
    assert attr.categories["tool_outputs"].tokens == 200


def test_per_category_tokens_reconcile_to_real_total():
    """Largest-remainder apportionment: integers sum to the real input total
    regardless of awkward weights/rounding."""
    segments = PromptSegments(
        user_prompt="u" * 333,
        coordination_context="c" * 777,
        tool_output_chars=999,
    )
    usage = TurnUsage(input_tokens=1234, cache_read_tokens=4321, output_tokens=50)
    attr = attribute_turn(segments, usage)
    input_sum = sum(
        c.tokens
        for k, c in attr.categories.items()
        if k != "thinking_and_output"
    )
    assert input_sum == usage.total_input_tokens
    assert attr.categories["thinking_and_output"].tokens == 50


def test_no_cache_estimates_system_from_segment():
    """When no cache is reported, system prompt is estimated and carved out of
    the fresh input alongside the other categories."""
    segments = PromptSegments(
        system_prompt="s" * 800,   # ~200 tok
        user_prompt="u" * 800,     # ~200 tok
    )
    usage = TurnUsage(input_tokens=400)  # no cache
    attr = attribute_turn(segments, usage)
    # 400 split 1:1 between system and user
    assert attr.categories["system_instructions"].tokens == 200
    assert attr.categories["user_messages"].tokens == 200


def test_no_segments_assigns_fresh_input_to_user():
    segments = PromptSegments()
    usage = TurnUsage(input_tokens=500, cache_read_tokens=100)
    attr = attribute_turn(segments, usage)
    assert attr.categories["user_messages"].tokens == 500
    assert attr.categories["system_instructions"].tokens == 100


def test_cost_apportioned_by_token_share():
    segments = PromptSegments(user_prompt="u" * 400)
    usage = TurnUsage(input_tokens=100, output_tokens=100, cost_usd=1.0)
    attr = attribute_turn(segments, usage)
    total_cost = sum(c.cost_usd for c in attr.categories.values())
    assert total_cost == pytest.approx(1.0, abs=1e-9)
    # 100 input (user) + 100 output -> each half the cost
    assert attr.categories["user_messages"].cost_usd == pytest.approx(0.5)
    assert attr.categories["thinking_and_output"].cost_usd == pytest.approx(0.5)


def test_pct_of_window_math():
    segments = PromptSegments(user_prompt="u" * 400)
    usage = TurnUsage(input_tokens=20_000)
    attr_dict = render_breakdown(
        {
            "version": 1,
            "turns": 1,
            "totalInputTokens": 20_000,
            "outputTokens": 0,
            "totalCostUsd": 0.0,
            "categories": {
                "user_messages": {"tokens": 20_000, "costUsd": 0.0},
            },
            "model": "claude-sonnet-4-5",
            "maxTokens": 200_000,
            "updatedAt": None,
        }
    )
    # 20k / 200k = 10%
    assert attr_dict["pctOfWindow"] == pytest.approx(10.0)
    assert _cat(attr_dict, "user_messages")["pctOfWindow"] == pytest.approx(10.0)


def test_context_window_for_model():
    assert context_window_for_model("claude-sonnet-4-5-20250929") == 200_000
    assert context_window_for_model("claude-opus-4-8[1m]") == 1_000_000
    assert context_window_for_model(None) == 200_000


def test_record_turn_persists_and_aggregates(tmp_path: Path):
    spec_dir = tmp_path / "001-feature"
    spec_dir.mkdir()

    seg = PromptSegments(user_prompt="u" * 400, tool_output_chars=400)
    usage = TurnUsage(input_tokens=200, output_tokens=50, cache_read_tokens=1000,
                      cost_usd=0.5)

    b1 = record_turn(spec_dir, seg, usage, model="claude-sonnet-4-5")
    assert b1["turns"] == 1
    assert usage_file_path(spec_dir).exists()

    # Second turn aggregates.
    b2 = record_turn(spec_dir, seg, usage, model="claude-sonnet-4-5")
    assert b2["turns"] == 2
    # Totals doubled.
    assert b2["totalInputTokens"] == 2 * b1["totalInputTokens"]
    assert b2["outputTokens"] == 100
    assert b2["totalCostUsd"] == pytest.approx(1.0)

    # Persisted file is valid JSON and matches read_breakdown.
    on_disk = json.loads(usage_file_path(spec_dir).read_text())
    assert on_disk["turns"] == 2
    assert read_breakdown(spec_dir)["turns"] == 2


def test_read_breakdown_empty_when_absent(tmp_path: Path):
    spec_dir = tmp_path / "002-empty"
    spec_dir.mkdir()
    b = read_breakdown(spec_dir)
    assert b["turns"] == 0
    assert b["totalInputTokens"] == 0
    # All canonical categories are present even when empty.
    keys = {c["key"] for c in b["categories"]}
    assert "system_instructions" in keys
    assert "tool_outputs" in keys


def test_record_turn_atomic_no_partial_file(tmp_path: Path):
    """A no-temp-file-left-behind check: only token_usage.json remains."""
    spec_dir = tmp_path / "003-atomic"
    spec_dir.mkdir()
    record_turn(spec_dir, PromptSegments(user_prompt="hi"), TurnUsage(input_tokens=10))
    leftovers = [p.name for p in spec_dir.iterdir()]
    assert leftovers == ["token_usage.json"]
