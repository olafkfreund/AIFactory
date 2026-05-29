"""Tests for per-org model allowlist enforcement (Epic #35 #38 PR-2b).

Covers (design §6):
- ``allowed_models=["claude-*"]`` accepts ``claude-opus-4-7``,
  rejects ``gpt-4o-mini``.
- ``allowed_models=["*"]`` (PR-2a schema default) accepts every model.
- ``allowed_models=["gpt-4o"]`` exact-matches that single model
  (no implicit glob).
- ``allowed_models=None`` (caller opted out of enforcement) accepts
  every model — keeps backward compat for CLI / test contexts.
- ``ModelNotAllowedError`` carries the org id + rejected model +
  allowlist for operator log-line clarity.
- The check runs at provider __init__, BEFORE any HTTP setup. A
  rejected model never opens a network connection.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / 'apps' / 'backend'
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


# ---------------------------------------------------------------------------
# fnmatch-based matching
# ---------------------------------------------------------------------------


def test_glob_pattern_accepts_matching_model(monkeypatch):
    monkeypatch.delenv('LITELLM_GATEWAY_URL', raising=False)
    from providers.openai_compatible import OpenAICompatibleProvider

    # Should not raise.
    p = OpenAICompatibleProvider(
        model='claude-opus-4-7',
        allowed_models=['claude-*'],
        org_id='org-acme',
    )
    assert p._model == 'claude-opus-4-7'


def test_glob_pattern_rejects_non_matching_model(monkeypatch):
    monkeypatch.delenv('LITELLM_GATEWAY_URL', raising=False)
    from providers.openai_compatible import (
        ModelNotAllowedError,
        OpenAICompatibleProvider,
    )

    with pytest.raises(ModelNotAllowedError) as excinfo:
        OpenAICompatibleProvider(
            model='gpt-4o-mini',
            allowed_models=['claude-*'],
            org_id='org-acme',
        )
    err = excinfo.value
    assert err.model == 'gpt-4o-mini'
    assert err.allowed_models == ['claude-*']
    assert err.org_id == 'org-acme'
    # Operator-actionable message.
    assert 'gpt-4o-mini' in str(err)
    assert 'org-acme' in str(err)


def test_wildcard_star_accepts_everything(monkeypatch):
    monkeypatch.delenv('LITELLM_GATEWAY_URL', raising=False)
    from providers.openai_compatible import OpenAICompatibleProvider

    # PR-2a schema default — backward compat.
    for model in ('gpt-4o-mini', 'claude-opus-4-7', 'bedrock/anthropic.claude-3'):
        p = OpenAICompatibleProvider(
            model=model, allowed_models=['*'], org_id='org-acme',
        )
        assert p._model == model


def test_none_allowlist_bypasses_enforcement(monkeypatch):
    """When the caller passes no allowlist (e.g. CLI mode), enforcement
    is off. Only callers that explicitly opt in (web-server provider
    factory keyed off the org row) drive enforcement."""
    monkeypatch.delenv('LITELLM_GATEWAY_URL', raising=False)
    from providers.openai_compatible import OpenAICompatibleProvider

    p = OpenAICompatibleProvider(model='gpt-4o-mini')  # no allowed_models
    assert p._model == 'gpt-4o-mini'


def test_exact_match_does_not_glob(monkeypatch):
    """``["gpt-4o"]`` is exact-match. ``"gpt-4o-mini"`` is rejected;
    operators must specify the glob form ``["gpt-4o*"]`` if they want
    prefix matching."""
    monkeypatch.delenv('LITELLM_GATEWAY_URL', raising=False)
    from providers.openai_compatible import (
        ModelNotAllowedError,
        OpenAICompatibleProvider,
    )

    # Exact match accepted.
    OpenAICompatibleProvider(model='gpt-4o', allowed_models=['gpt-4o'])

    # Prefix without glob rejected.
    with pytest.raises(ModelNotAllowedError):
        OpenAICompatibleProvider(
            model='gpt-4o-mini', allowed_models=['gpt-4o'],
        )


def test_multiple_patterns_any_match_accepts(monkeypatch):
    monkeypatch.delenv('LITELLM_GATEWAY_URL', raising=False)
    from providers.openai_compatible import OpenAICompatibleProvider

    p = OpenAICompatibleProvider(
        model='claude-sonnet-4-5',
        allowed_models=['gpt-4o*', 'claude-sonnet-*'],
    )
    assert p._model == 'claude-sonnet-4-5'


def test_enforcement_runs_before_http_setup(monkeypatch):
    """Verify the allowlist check happens at __init__ before any
    network setup. A rejected model must NOT open a connection."""
    monkeypatch.delenv('LITELLM_GATEWAY_URL', raising=False)
    from providers.openai_compatible import (
        ModelNotAllowedError,
        OpenAICompatibleProvider,
    )

    # If enforcement ran AFTER HTTP setup, we'd see a urllib call here.
    # The pytest assertion is just that the exception type is the
    # allowlist one, not a network-layer one.
    with pytest.raises(ModelNotAllowedError):
        OpenAICompatibleProvider(
            model='banned-model',
            allowed_models=['gpt-*'],
            base_url='http://invalid-host-that-does-not-resolve.example',
        )
