"""Tests for per-provider billing-mode classification (#96)."""

from __future__ import annotations

from server.services.billing import classify_billing_mode, is_metered


def test_subscription_when_no_api_key():
    env: dict[str, str] = {}
    for p in ("claude", "codex", "antigravity", "gemini"):
        assert classify_billing_mode(p, env) == "subscription"


def test_api_when_provider_api_key_present():
    assert classify_billing_mode("claude", {"ANTHROPIC_API_KEY": "sk-x"}) == "api"
    assert classify_billing_mode("codex", {"OPENAI_API_KEY": "sk-x"}) == "api"
    assert classify_billing_mode("gemini", {"GOOGLE_API_KEY": "x"}) == "api"
    # An unrelated key does not flip an unrelated provider.
    assert classify_billing_mode("claude", {"OPENAI_API_KEY": "x"}) == "subscription"


def test_copilot_and_opencode_are_always_subscription():
    # Their token is the subscription, not a metered API key.
    assert classify_billing_mode("copilot", {"GITHUB_TOKEN": "x"}) == "subscription"
    assert classify_billing_mode("opencode", {}) == "subscription"


def test_ollama_local_vs_cloud():
    assert classify_billing_mode("ollama", {}) == "local"  # default localhost
    assert (
        classify_billing_mode("ollama", {"OLLAMA_HOST": "http://localhost:11434"})
        == "local"
    )
    assert (
        classify_billing_mode("ollama", {"OLLAMA_HOST": "127.0.0.1:11434"}) == "local"
    )
    assert (
        classify_billing_mode("ollama", {"OLLAMA_HOST": "https://ollama.com"})
        == "cloud"
    )
    assert (
        classify_billing_mode(
            "local-ollama", {"OLLAMA_BASE_URL": "http://gpu-box.lan:11434"}
        )
        == "cloud"
    )


def test_inherently_metered_providers():
    assert classify_billing_mode("openai-compatible", {}) == "api"
    assert classify_billing_mode("github-models", {}) == "api"


def test_unknown_and_empty():
    assert classify_billing_mode(None, {}) == "unknown"
    assert classify_billing_mode("", {}) == "unknown"
    assert classify_billing_mode("some-new-provider", {}) == "unknown"


def test_is_metered():
    assert is_metered("api") and is_metered("cloud")
    assert not is_metered("subscription")
    assert not is_metered("local")
    assert not is_metered("unknown")
    assert not is_metered(None)
