"""Regression tests for the OpenAI-compatible → LiteLLM-routed path.

Two defects fixed here (Bedrock / Azure / Vertex via LiteLLM gateway):

1. ``OpenAICompatibleAgenticProvider`` never honoured ``LITELLM_GATEWAY_URL``
   (it did ``base_url.rstrip("/")`` directly, unlike the text-only
   ``OpenAICompatibleProvider`` which calls ``_gateway.resolve_base_url``).
   Because EVERY real build phase (spec/planning/coding/qa/qa_fixer) uses the
   *agentic* provider, the gateway redirect was effectively dead for the whole
   pipeline — calls went straight to the native endpoint, bypassing the
   gateway's budget / allowlist / audit and never reaching Bedrock/Vertex.

2. ``get_provider_extra_kwargs`` returned no ``api_key`` for ``bedrock/*`` /
   ``vertex_ai/*`` models, so the provider sent no ``Authorization: Bearer``
   header.  A production LiteLLM proxy (master key / virtual keys configured)
   rejects unauthenticated data-plane calls with 401.

These tests mock the HTTP leg — no real network/creds required.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

_BACKEND = Path(__file__).parent.parent / "apps" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

_GATEWAY = "http://litellm:4000"


# ---------------------------------------------------------------------------
# Defect 1: agentic provider honours the gateway (it's what build phases use)
# ---------------------------------------------------------------------------


def test_agentic_uses_native_default_when_env_unset(monkeypatch):
    from providers.openai_compatible_agentic import (
        OpenAICompatibleAgenticProvider,
    )

    monkeypatch.delenv("LITELLM_GATEWAY_URL", raising=False)
    p = OpenAICompatibleAgenticProvider(model="gpt-4o-mini", working_dir="/tmp")
    assert p._base_url == "https://api.openai.com"


def test_agentic_redirects_when_env_set(monkeypatch):
    from providers.openai_compatible_agentic import (
        OpenAICompatibleAgenticProvider,
    )

    monkeypatch.setenv("LITELLM_GATEWAY_URL", _GATEWAY)
    p = OpenAICompatibleAgenticProvider(model="gpt-4o-mini", working_dir="/tmp")
    assert p._base_url == _GATEWAY


def test_agentic_env_overrides_explicit_base_url(monkeypatch):
    """Env wins over per-call base_url — same enforcement contract as the
    text-only provider (operators can't be bypassed by per-call config)."""
    from providers.openai_compatible_agentic import (
        OpenAICompatibleAgenticProvider,
    )

    monkeypatch.setenv("LITELLM_GATEWAY_URL", _GATEWAY)
    p = OpenAICompatibleAgenticProvider(
        model="gpt-4o-mini",
        base_url="https://openrouter.ai/api",
        working_dir="/tmp",
    )
    assert p._base_url == _GATEWAY


@pytest.mark.parametrize(
    "gateway",
    ["http://litellm:4000", "http://litellm:4000/", "http://litellm:4000///"],
)
def test_agentic_strips_trailing_slashes_on_gateway(monkeypatch, gateway):
    from providers.openai_compatible_agentic import (
        OpenAICompatibleAgenticProvider,
    )

    monkeypatch.setenv("LITELLM_GATEWAY_URL", gateway)
    p = OpenAICompatibleAgenticProvider(model="gpt-4o-mini", working_dir="/tmp")
    assert p._base_url == "http://litellm:4000"


# ---------------------------------------------------------------------------
# Defect 1 (end-to-end): bedrock/* through get_provider_extra_kwargs lands on
# the gateway for the AGENTIC provider, with the prefix preserved.
# ---------------------------------------------------------------------------


def test_agentic_bedrock_routes_through_gateway(monkeypatch):
    from phase_config import get_provider_extra_kwargs
    from providers.openai_compatible_agentic import (
        OpenAICompatibleAgenticProvider,
    )

    monkeypatch.setenv("LITELLM_GATEWAY_URL", _GATEWAY)
    monkeypatch.setenv("LITELLM_API_KEY", "sk-test")
    model = "bedrock/anthropic.claude-sonnet-4-20250514-v1:0"
    kwargs = get_provider_extra_kwargs("openai-compatible", model)

    p = OpenAICompatibleAgenticProvider(working_dir="/tmp", **kwargs)
    assert p._base_url == _GATEWAY
    # Prefix preserved — LiteLLM resolves the backend from the model string.
    assert p._model == model


def test_agentic_vertex_routes_through_gateway(monkeypatch):
    from phase_config import get_provider_extra_kwargs
    from providers.openai_compatible_agentic import (
        OpenAICompatibleAgenticProvider,
    )

    monkeypatch.setenv("LITELLM_GATEWAY_URL", _GATEWAY)
    monkeypatch.setenv("LITELLM_API_KEY", "sk-test")
    model = "vertex_ai/gemini-2.5-pro"
    kwargs = get_provider_extra_kwargs("openai-compatible", model)

    p = OpenAICompatibleAgenticProvider(working_dir="/tmp", **kwargs)
    assert p._base_url == _GATEWAY
    assert p._model == model


def test_agentic_studio_redirects_to_gateway_when_enabled(monkeypatch):
    """studio:* picks the Google native base_url, but with the gateway enabled
    the agentic provider must still route through LiteLLM (it didn't before)."""
    from phase_config import get_provider_extra_kwargs
    from providers.openai_compatible_agentic import (
        OpenAICompatibleAgenticProvider,
    )

    monkeypatch.setenv("LITELLM_GATEWAY_URL", _GATEWAY)
    kwargs = get_provider_extra_kwargs("openai-compatible", "studio:gemini-2.5-flash")
    # The kwargs carry the Google native default...
    assert kwargs["base_url"].startswith("https://generativelanguage.googleapis.com")
    # ...but the provider redirects to the gateway.
    p = OpenAICompatibleAgenticProvider(working_dir="/tmp", **kwargs)
    assert p._base_url == _GATEWAY


# ---------------------------------------------------------------------------
# Defect 2: bedrock/vertex carry a Bearer key so LiteLLM doesn't 401.
# ---------------------------------------------------------------------------


def test_bedrock_extra_kwargs_carries_litellm_api_key(monkeypatch):
    from phase_config import get_provider_extra_kwargs

    monkeypatch.setenv("LITELLM_GATEWAY_URL", _GATEWAY)
    monkeypatch.setenv("LITELLM_API_KEY", "sk-litellm-master")
    monkeypatch.delenv("OPENAI_COMPATIBLE_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    kwargs = get_provider_extra_kwargs(
        "openai-compatible", "bedrock/anthropic.claude-sonnet-4-20250514-v1:0"
    )
    assert kwargs["api_key"] == "sk-litellm-master"


def test_vertex_extra_kwargs_falls_back_to_openai_compatible_key(monkeypatch):
    from phase_config import get_provider_extra_kwargs

    monkeypatch.setenv("LITELLM_GATEWAY_URL", _GATEWAY)
    monkeypatch.delenv("LITELLM_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", "sk-compat")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    kwargs = get_provider_extra_kwargs("openai-compatible", "vertex_ai/gemini-2.5-pro")
    assert kwargs["api_key"] == "sk-compat"


def test_bedrock_extra_kwargs_api_key_none_when_no_env(monkeypatch):
    """No key env at all → api_key None (local/keyless LiteLLM dev proxy)."""
    from phase_config import get_provider_extra_kwargs

    monkeypatch.setenv("LITELLM_GATEWAY_URL", _GATEWAY)
    monkeypatch.delenv("LITELLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_COMPATIBLE_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    kwargs = get_provider_extra_kwargs(
        "openai-compatible", "bedrock/anthropic.claude-haiku-4-0"
    )
    assert kwargs["api_key"] is None


def test_bedrock_provider_sends_authorization_header(monkeypatch):
    """The constructed agentic provider sends Authorization: Bearer <key>
    and POSTs the bedrock-prefixed model to the gateway's chat endpoint.

    The HTTP leg is mocked: we capture the urllib Request and assert on the
    URL, headers, and JSON body — no network/creds required.
    """
    from phase_config import get_provider_extra_kwargs
    from providers.openai_compatible_agentic import (
        OpenAICompatibleAgenticProvider,
    )

    monkeypatch.setenv("LITELLM_GATEWAY_URL", _GATEWAY)
    monkeypatch.setenv("LITELLM_API_KEY", "sk-test-key")
    model = "bedrock/anthropic.claude-sonnet-4-20250514-v1:0"
    kwargs = get_provider_extra_kwargs("openai-compatible", model)
    p = OpenAICompatibleAgenticProvider(working_dir="/tmp", **kwargs)

    captured: dict[str, Any] = {}

    def fake_http_post(url: str, payload: dict[str, Any]) -> dict[str, Any]:
        captured["url"] = url
        captured["headers"] = p._build_headers()
        captured["payload"] = payload
        # Minimal OpenAI-shape reply with no tool calls → loop terminates.
        return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(p, "_http_post", fake_http_post)

    import asyncio

    async def drive() -> None:
        await p.query("hello")
        async for _ in p.receive_response():
            pass

    asyncio.run(drive())

    assert captured["url"] == f"{_GATEWAY}/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer sk-test-key"
    # Model forwarded in LiteLLM's expected bedrock/<id> form (prefix kept).
    assert captured["payload"]["model"] == model
    # Sanity: the body is real JSON-serialisable OpenAI shape.
    json.dumps(captured["payload"])


# ---------------------------------------------------------------------------
# Regression guard: local / studio / hosted OpenAI-compatible paths unchanged
# when the gateway is NOT configured.
# ---------------------------------------------------------------------------


def test_studio_unchanged_without_gateway(monkeypatch):
    from phase_config import get_provider_extra_kwargs
    from providers.openai_compatible_agentic import (
        OpenAICompatibleAgenticProvider,
    )

    monkeypatch.delenv("LITELLM_GATEWAY_URL", raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "g-key")
    kwargs = get_provider_extra_kwargs("openai-compatible", "studio:gemini-2.5-flash")
    p = OpenAICompatibleAgenticProvider(working_dir="/tmp", **kwargs)
    assert p._base_url.startswith("https://generativelanguage.googleapis.com")
    assert p._model == "gemini-2.5-flash"


def test_local_openai_compat_unchanged_without_gateway(monkeypatch):
    """A local vLLM/LM-Studio base_url is honoured verbatim when no gateway."""
    from providers.openai_compatible_agentic import (
        OpenAICompatibleAgenticProvider,
    )

    monkeypatch.delenv("LITELLM_GATEWAY_URL", raising=False)
    p = OpenAICompatibleAgenticProvider(
        model="qwen2.5-coder-32b",
        base_url="http://localhost:1234",
        working_dir="/tmp",
    )
    assert p._base_url == "http://localhost:1234"
