"""Tests for Bedrock + Vertex AI routing via LiteLLM (Epic #35 #39).

Covers:
- bedrock/* model strings are inferred as 'openai-compatible' provider.
- vertex_ai/* model strings are inferred as 'openai-compatible' provider.
- With LITELLM_GATEWAY_URL set, get_provider_extra_kwargs returns the
  gateway URL as base_url and the full model string (including prefix).
- Without LITELLM_GATEWAY_URL, get_provider_extra_kwargs raises a clear
  ValueError pointing operators at the concept doc.
- The OpenAICompatibleProvider constructed for these models carries the
  gateway base_url.
- Other model strings (claude-*, gpt-*, gemini-*) are not affected.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).parent.parent / "apps" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


# ---------------------------------------------------------------------------
# infer_provider_from_model routing
# ---------------------------------------------------------------------------


def test_bedrock_claude_sonnet_routes_to_openai_compatible(monkeypatch):
    from phase_config import infer_provider_from_model

    assert (
        infer_provider_from_model("bedrock/anthropic.claude-sonnet-4-20250514-v1:0")
        == "openai-compatible"
    )


def test_bedrock_llama_routes_to_openai_compatible(monkeypatch):
    from phase_config import infer_provider_from_model

    assert (
        infer_provider_from_model("bedrock/meta.llama3-1-70b-instruct-v1:0")
        == "openai-compatible"
    )


def test_vertex_ai_gemini_routes_to_openai_compatible(monkeypatch):
    from phase_config import infer_provider_from_model

    assert infer_provider_from_model("vertex_ai/gemini-2.5-pro") == "openai-compatible"


def test_vertex_ai_claude_routes_to_openai_compatible(monkeypatch):
    from phase_config import infer_provider_from_model

    assert (
        infer_provider_from_model("vertex_ai/claude-sonnet-4@20250514")
        == "openai-compatible"
    )


def test_bedrock_prefix_case_insensitive(monkeypatch):
    # Model strings with mixed case should still route correctly.
    from phase_config import infer_provider_from_model

    assert (
        infer_provider_from_model("BEDROCK/anthropic.claude-haiku-4-0")
        == "openai-compatible"
    )


def test_vertex_ai_prefix_case_insensitive(monkeypatch):
    from phase_config import infer_provider_from_model

    assert infer_provider_from_model("VERTEX_AI/gemini-flash") == "openai-compatible"


# ---------------------------------------------------------------------------
# Other model strings remain unaffected
# ---------------------------------------------------------------------------


def test_claude_model_unaffected(monkeypatch):
    from phase_config import infer_provider_from_model

    assert infer_provider_from_model("claude-sonnet-4-6") == "claude"


def test_gpt_model_unaffected(monkeypatch):
    from phase_config import infer_provider_from_model

    assert infer_provider_from_model("gpt-4o") == "codex"


def test_gemini_model_unaffected(monkeypatch):
    from phase_config import infer_provider_from_model

    # gemini-* models now route to the canonical "antigravity" provider
    # (the Antigravity CLI serves Google's gemini-* models). Back-compat for
    # the legacy provider name is via the factory aliases, not this function.
    assert infer_provider_from_model("gemini-2.5-pro") == "antigravity"
    assert infer_provider_from_model("antigravity") == "antigravity"
    assert infer_provider_from_model("antigravity-pro") == "antigravity"


# ---------------------------------------------------------------------------
# get_provider_extra_kwargs — with gateway configured
# ---------------------------------------------------------------------------


def test_bedrock_extra_kwargs_with_gateway(monkeypatch):
    """When the gateway is set, bedrock/* gets the gateway URL as base_url
    and the full model string (including prefix) is forwarded to LiteLLM."""
    from phase_config import get_provider_extra_kwargs

    monkeypatch.setenv("LITELLM_GATEWAY_URL", "http://litellm:4000")
    model = "bedrock/anthropic.claude-sonnet-4-20250514-v1:0"
    kwargs = get_provider_extra_kwargs("openai-compatible", model)

    assert kwargs["base_url"] == "http://litellm:4000"
    assert kwargs["model"] == model


def test_vertex_ai_extra_kwargs_with_gateway(monkeypatch):
    """vertex_ai/* forwarded as-is so LiteLLM resolves the Vertex endpoint."""
    from phase_config import get_provider_extra_kwargs

    monkeypatch.setenv("LITELLM_GATEWAY_URL", "http://litellm:4000")
    model = "vertex_ai/gemini-2.5-pro"
    kwargs = get_provider_extra_kwargs("openai-compatible", model)

    assert kwargs["base_url"] == "http://litellm:4000"
    assert kwargs["model"] == model


def test_bedrock_llama_extra_kwargs_with_gateway(monkeypatch):
    from phase_config import get_provider_extra_kwargs

    monkeypatch.setenv("LITELLM_GATEWAY_URL", "http://litellm:4000")
    model = "bedrock/meta.llama3-1-70b-instruct-v1:0"
    kwargs = get_provider_extra_kwargs("openai-compatible", model)

    assert kwargs["base_url"] == "http://litellm:4000"
    assert kwargs["model"] == model


def test_vertex_claude_extra_kwargs_with_gateway(monkeypatch):
    from phase_config import get_provider_extra_kwargs

    monkeypatch.setenv("LITELLM_GATEWAY_URL", "http://litellm:4000")
    model = "vertex_ai/claude-sonnet-4@20250514"
    kwargs = get_provider_extra_kwargs("openai-compatible", model)

    assert kwargs["base_url"] == "http://litellm:4000"
    assert kwargs["model"] == model


# ---------------------------------------------------------------------------
# get_provider_extra_kwargs — without gateway: clear error
# ---------------------------------------------------------------------------


def test_bedrock_without_gateway_raises_clear_error(monkeypatch):
    """Constructing a bedrock/* provider without LITELLM_GATEWAY_URL must
    raise a ValueError with an operator-actionable message rather than
    producing a cryptic OpenAI 404."""
    from phase_config import get_provider_extra_kwargs

    monkeypatch.delenv("LITELLM_GATEWAY_URL", raising=False)

    with pytest.raises(ValueError) as exc_info:
        get_provider_extra_kwargs(
            "openai-compatible",
            "bedrock/anthropic.claude-sonnet-4-20250514-v1:0",
        )

    msg = str(exc_info.value)
    assert "LITELLM_GATEWAY_URL" in msg
    assert "cloud-llm-routing" in msg


def test_vertex_ai_without_gateway_raises_clear_error(monkeypatch):
    from phase_config import get_provider_extra_kwargs

    monkeypatch.delenv("LITELLM_GATEWAY_URL", raising=False)

    with pytest.raises(ValueError) as exc_info:
        get_provider_extra_kwargs(
            "openai-compatible",
            "vertex_ai/gemini-2.5-pro",
        )

    msg = str(exc_info.value)
    assert "LITELLM_GATEWAY_URL" in msg
    assert "cloud-llm-routing" in msg


def test_whitespace_gateway_url_treated_as_unset(monkeypatch):
    """An env var set to spaces should trigger the same clear error —
    same behaviour as _gateway.is_gateway_enabled()."""
    from phase_config import get_provider_extra_kwargs

    monkeypatch.setenv("LITELLM_GATEWAY_URL", "   ")

    with pytest.raises(ValueError) as exc_info:
        get_provider_extra_kwargs(
            "openai-compatible",
            "bedrock/anthropic.claude-haiku-4-0",
        )

    assert "LITELLM_GATEWAY_URL" in str(exc_info.value)


# ---------------------------------------------------------------------------
# OpenAICompatibleProvider integration — gateway base_url is set
# ---------------------------------------------------------------------------


def test_openai_compatible_provider_uses_gateway_for_bedrock(monkeypatch):
    """End-to-end: provider instantiated for a bedrock/* model carries
    the gateway URL as its internal base_url."""
    from phase_config import get_provider_extra_kwargs
    from providers.openai_compatible import OpenAICompatibleProvider

    monkeypatch.setenv("LITELLM_GATEWAY_URL", "http://litellm:4000")
    model = "bedrock/anthropic.claude-sonnet-4-20250514-v1:0"
    kwargs = get_provider_extra_kwargs("openai-compatible", model)

    provider = OpenAICompatibleProvider(**kwargs)
    assert provider._base_url == "http://litellm:4000"
    assert provider._model == model


def test_openai_compatible_provider_uses_gateway_for_vertex(monkeypatch):
    from phase_config import get_provider_extra_kwargs
    from providers.openai_compatible import OpenAICompatibleProvider

    monkeypatch.setenv("LITELLM_GATEWAY_URL", "http://litellm:4000")
    model = "vertex_ai/gemini-2.5-pro"
    kwargs = get_provider_extra_kwargs("openai-compatible", model)

    provider = OpenAICompatibleProvider(**kwargs)
    assert provider._base_url == "http://litellm:4000"
    assert provider._model == model
