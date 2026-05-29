"""Tests for Claude enforcement — streaming usage capture (Epic #35 v1.2 #207).

Covers design §6:
- Token counts accumulated correctly from stream messages.
- Partial usage captured on early termination (abandoned path).
- Cost estimated from _CLAUDE_PRICING table.
- Unknown model -> estimated_cost_usd() returns None.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for _root in (REPO_ROOT / 'apps' / 'web-server', REPO_ROOT / 'apps' / 'backend'):
    if str(_root) not in sys.path:
        sys.path.insert(0, str(_root))


class _FakeUsage:
    def __init__(self, input_tokens=None, output_tokens=None,
                 cache_read_tokens=None, cache_creation_tokens=None):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_read_tokens = cache_read_tokens
        self.cache_creation_tokens = cache_creation_tokens


class _FakeMsg:
    def __init__(self, usage=None, content=None):
        self.usage = usage
        self.content = content or []
        self.type = 'result'


def test_usage_captured_from_stream_message():
    from core.enforcement import ClaudeUsageSnapshot, _EnforcedClaudeSDKClient

    usage = ClaudeUsageSnapshot(model='claude-opus-4-7')

    class _Parent:
        _response_text = ''

    wrapped = _EnforcedClaudeSDKClient._WrappedResponseStream(
        inner=None,  # not used in _capture
        usage=usage,
        parent=_Parent(),
    )

    msg = _FakeMsg(usage=_FakeUsage(input_tokens=500, output_tokens=200))
    wrapped._capture(msg)

    assert usage.input_tokens == 500
    assert usage.output_tokens == 200


def test_cache_tokens_captured():
    from core.enforcement import ClaudeUsageSnapshot, _EnforcedClaudeSDKClient

    usage = ClaudeUsageSnapshot(model='claude-sonnet-4-6')

    class _Parent:
        _response_text = ''

    wrapped = _EnforcedClaudeSDKClient._WrappedResponseStream(
        inner=None, usage=usage, parent=_Parent(),
    )
    msg = _FakeMsg(usage=_FakeUsage(
        input_tokens=1000, output_tokens=300,
        cache_read_tokens=200, cache_creation_tokens=50,
    ))
    wrapped._capture(msg)

    assert usage.cache_read_tokens == 200
    assert usage.cache_creation_tokens == 50


def test_cost_estimate_opus_47():
    from core.enforcement import _CLAUDE_PRICING, ClaudeUsageSnapshot

    snap = ClaudeUsageSnapshot(model='claude-opus-4-7')
    snap.input_tokens = 1_000_000
    snap.output_tokens = 1_000_000

    cost = snap.estimated_cost_usd()
    rate = _CLAUDE_PRICING['claude-opus-4-7']
    expected = rate['input'] + rate['output']  # per-million each
    assert abs(cost - expected) < 1e-9


def test_cost_estimate_with_cache():
    from core.enforcement import _CLAUDE_PRICING, ClaudeUsageSnapshot

    snap = ClaudeUsageSnapshot(model='claude-sonnet-4-6')
    snap.input_tokens = 1_000_000
    snap.output_tokens = 0
    snap.cache_read_tokens = 1_000_000
    snap.cache_creation_tokens = 0

    cost = snap.estimated_cost_usd()
    rate = _CLAUDE_PRICING['claude-sonnet-4-6']
    expected = rate['input'] + rate['cache_read']
    assert abs(cost - expected) < 1e-9


def test_unknown_model_returns_none():
    from core.enforcement import ClaudeUsageSnapshot

    snap = ClaudeUsageSnapshot(model='gpt-4o')
    snap.input_tokens = 1000
    snap.output_tokens = 500
    assert snap.estimated_cost_usd() is None


def test_none_input_tokens_returns_none():
    from core.enforcement import ClaudeUsageSnapshot

    snap = ClaudeUsageSnapshot(model='claude-opus-4-7')
    snap.input_tokens = None
    snap.output_tokens = 100
    assert snap.estimated_cost_usd() is None


def test_response_text_accumulated():
    from core.enforcement import ClaudeUsageSnapshot, _EnforcedClaudeSDKClient

    usage = ClaudeUsageSnapshot(model='claude-opus-4-7')

    class _Parent:
        _response_text = ''

    wrapped = _EnforcedClaudeSDKClient._WrappedResponseStream(
        inner=None, usage=usage, parent=_Parent(),
    )

    class _TextBlock:
        text = 'hello world'

    msg = _FakeMsg(content=[_TextBlock()])
    wrapped._capture(msg)
    assert 'hello world' in wrapped._parent._response_text


def test_all_production_models_in_pricing_table():
    """Every Claude model used in phase_config.py must have a _CLAUDE_PRICING entry.

    This is recommendation #1 from the design reviewer — catches "added a
    new model, forgot to bump the pricing dict."
    """
    import sys as _sys
    _sys.path.insert(0, str(REPO_ROOT / 'apps' / 'backend'))
    from core.enforcement import _CLAUDE_PRICING
    from phase_config import ADAPTIVE_THINKING_MODELS, MODEL_ID_MAP

    # Collect all Claude model IDs referenced in phase_config.
    all_model_ids = set(MODEL_ID_MAP.values()) | ADAPTIVE_THINKING_MODELS
    claude_ids = {m for m in all_model_ids if m.startswith('claude-')}

    missing = claude_ids - set(_CLAUDE_PRICING)
    assert not missing, (
        f'_CLAUDE_PRICING is missing entries for: {sorted(missing)}. '
        f'Add the per-million-token rates from the Anthropic pricing page.'
    )
