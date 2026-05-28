"""Tests for LiteLLM gateway redirect (Epic #35 #38 PR-1).

Covers:
- `_gateway.resolve_base_url` returns native when env unset.
- `_gateway.resolve_base_url` returns gateway URL when env set.
- `_gateway.is_gateway_enabled` reflects env state.
- `_gateway.LiteLLMGatewayUnavailableError` carries the gateway URL
  + underlying exception + operator-actionable hint.
- `OpenAICompatibleProvider` respects the env var at instantiation
  (base_url defaults swapped).
- `OllamaProvider` respects the env var the same way.
- Explicit `base_url` constructor arg is OVERRIDDEN by the env var
  when set (env wins; that's the v1.1 enforcement contract).
- Whitespace-only env var is treated as unset.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).parent.parent / "apps" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


# ---------------------------------------------------------------------------
# resolve_base_url
# ---------------------------------------------------------------------------


def test_resolve_returns_native_when_unset(monkeypatch):
    """No env → caller's native default applies."""
    from providers._gateway import resolve_base_url

    monkeypatch.delenv("LITELLM_GATEWAY_URL", raising=False)
    assert resolve_base_url("https://api.openai.com") == "https://api.openai.com"


def test_resolve_returns_gateway_when_set(monkeypatch):
    """Env set → gateway URL wins."""
    from providers._gateway import resolve_base_url

    monkeypatch.setenv("LITELLM_GATEWAY_URL", "http://litellm:4000")
    assert resolve_base_url("https://api.openai.com") == "http://litellm:4000"


def test_resolve_treats_whitespace_as_unset(monkeypatch):
    """An operator typo (env var set to spaces) shouldn't accidentally
    redirect — that'd produce a confusing AttributeError downstream."""
    from providers._gateway import resolve_base_url

    monkeypatch.setenv("LITELLM_GATEWAY_URL", "   ")
    assert resolve_base_url("https://api.openai.com") == "https://api.openai.com"


def test_is_gateway_enabled_matches_env(monkeypatch):
    from providers._gateway import is_gateway_enabled

    monkeypatch.delenv("LITELLM_GATEWAY_URL", raising=False)
    assert is_gateway_enabled() is False

    monkeypatch.setenv("LITELLM_GATEWAY_URL", "http://litellm:4000")
    assert is_gateway_enabled() is True


# ---------------------------------------------------------------------------
# LiteLLMGatewayUnavailableError
# ---------------------------------------------------------------------------


def test_unavailable_error_carries_url_and_underlying():
    from providers._gateway import LiteLLMGatewayUnavailableError

    underlying = RuntimeError("connection refused")
    exc = LiteLLMGatewayUnavailableError("http://litellm:4000", underlying)
    assert exc.gateway_url == "http://litellm:4000"
    assert exc.underlying is underlying

    msg = str(exc)
    assert "http://litellm:4000" in msg
    # Operator-actionable diagnostic in the message.
    assert "kubectl logs" in msg


# ---------------------------------------------------------------------------
# OpenAICompatibleProvider integration
# ---------------------------------------------------------------------------


def test_openai_compatible_uses_native_default_when_env_unset(monkeypatch):
    from providers.openai_compatible import OpenAICompatibleProvider

    monkeypatch.delenv("LITELLM_GATEWAY_URL", raising=False)
    p = OpenAICompatibleProvider(model="gpt-4o-mini")
    assert p._base_url == "https://api.openai.com"


def test_openai_compatible_redirects_when_env_set(monkeypatch):
    from providers.openai_compatible import OpenAICompatibleProvider

    monkeypatch.setenv("LITELLM_GATEWAY_URL", "http://litellm:4000")
    p = OpenAICompatibleProvider(model="gpt-4o-mini")
    assert p._base_url == "http://litellm:4000"


def test_openai_compatible_env_overrides_explicit_base_url(monkeypatch):
    """Env var WINS over the explicit base_url arg — that's the
    enforcement contract: operators flipping the gateway on should
    not be bypassable by per-call config."""
    from providers.openai_compatible import OpenAICompatibleProvider

    monkeypatch.setenv("LITELLM_GATEWAY_URL", "http://litellm:4000")
    p = OpenAICompatibleProvider(
        model="gpt-4o-mini", base_url="https://openrouter.ai/api",
    )
    assert p._base_url == "http://litellm:4000"


# ---------------------------------------------------------------------------
# OllamaProvider integration
# ---------------------------------------------------------------------------


def test_ollama_uses_native_default_when_env_unset(monkeypatch):
    from providers.ollama import OllamaProvider

    monkeypatch.delenv("LITELLM_GATEWAY_URL", raising=False)
    p = OllamaProvider(model="llama3.1:8b")
    assert p._base_url == "http://localhost:11434"


def test_ollama_redirects_when_env_set(monkeypatch):
    from providers.ollama import OllamaProvider

    monkeypatch.setenv("LITELLM_GATEWAY_URL", "http://litellm:4000")
    p = OllamaProvider(model="llama3.1:8b")
    assert p._base_url == "http://litellm:4000"


def test_ollama_env_overrides_explicit_base_url(monkeypatch):
    from providers.ollama import OllamaProvider

    monkeypatch.setenv("LITELLM_GATEWAY_URL", "http://litellm:4000")
    p = OllamaProvider(
        model="llama3.1:8b", base_url="http://my-ollama:8080",
    )
    assert p._base_url == "http://litellm:4000"


# ---------------------------------------------------------------------------
# Trailing-slash hygiene (matches existing provider invariant)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("gateway", [
    "http://litellm:4000",
    "http://litellm:4000/",
    "http://litellm:4000///",
])
def test_provider_strips_trailing_slashes_on_gateway(monkeypatch, gateway):
    """Operators may copy/paste URLs with stray trailing slashes;
    the existing trailing-slash strip still applies post-redirect."""
    from providers.openai_compatible import OpenAICompatibleProvider

    monkeypatch.setenv("LITELLM_GATEWAY_URL", gateway)
    p = OpenAICompatibleProvider(model="gpt-4o-mini")
    assert p._base_url == "http://litellm:4000"
