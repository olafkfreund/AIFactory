"""Tests for GitHub Models provider (AIFactory#457 / Component 1).

Covers:
- infer_provider_from_model() returns "github-models" for github-models/* strings
- infer_provider_from_model() does NOT return "github-models" for bare gpt-* (still codex)
- strip_provider_prefix() strips "github-models/" prefix correctly
- providers/factory.py: github-models alias resolves to "github-models" canonical
- providers/factory.py: get_provider() injects correct base_url/api_key and strips prefix
- get_provider() with github-models/* model does not raise
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_BACKEND = Path(__file__).parent.parent / "apps" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


# ---------------------------------------------------------------------------
# phase_config.infer_provider_from_model
# ---------------------------------------------------------------------------


def test_infer_github_models_full_path():
    from phase_config import infer_provider_from_model

    assert infer_provider_from_model("github-models/openai/gpt-4.1") == "github-models"


def test_infer_github_models_meta_llama():
    from phase_config import infer_provider_from_model

    assert (
        infer_provider_from_model("github-models/meta/llama-3.3-70b") == "github-models"
    )


def test_infer_github_models_uppercase_resilience():
    from phase_config import infer_provider_from_model

    # The function lowercases; verify it doesn't mis-route
    assert infer_provider_from_model("GitHub-Models/openai/gpt-4.1") == "github-models"


def test_infer_bare_gpt_still_routes_to_codex():
    """Bare gpt-4.1 must NOT route to github-models — it stays on codex."""
    from phase_config import infer_provider_from_model

    assert infer_provider_from_model("gpt-4.1") == "codex"


def test_infer_bare_o4_mini_still_routes_to_codex():
    from phase_config import infer_provider_from_model

    assert infer_provider_from_model("o4-mini") == "codex"


# ---------------------------------------------------------------------------
# phase_config.strip_provider_prefix
# ---------------------------------------------------------------------------


def test_strip_github_models_prefix():
    from phase_config import strip_provider_prefix

    assert strip_provider_prefix("github-models/openai/gpt-4.1") == "openai/gpt-4.1"


def test_strip_github_models_prefix_meta():
    from phase_config import strip_provider_prefix

    assert (
        strip_provider_prefix("github-models/meta/llama-3.3-70b")
        == "meta/llama-3.3-70b"
    )


def test_strip_does_not_touch_other_prefixes():
    from phase_config import strip_provider_prefix

    assert strip_provider_prefix("ollama:qwen3:14b") == "qwen3:14b"
    assert strip_provider_prefix("claude-sonnet-4-6") == "claude-sonnet-4-6"


# ---------------------------------------------------------------------------
# providers/factory.py alias resolution
# ---------------------------------------------------------------------------


def test_github_models_alias_resolves():
    from providers.factory import _resolve_canonical

    assert _resolve_canonical("github-models") == "github-models"
    assert _resolve_canonical("gh-models") == "github-models"


def test_github_alias_not_added():
    """'github' must NOT be an alias — it would shadow gh CLI integration."""
    from providers.factory import _PROVIDER_ALIASES

    assert "github" not in _PROVIDER_ALIASES


# ---------------------------------------------------------------------------
# providers/factory.get_provider — injection logic
# ---------------------------------------------------------------------------


def test_get_provider_github_models_injects_base_url(monkeypatch):
    """get_provider injects the correct base_url for github-models."""
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test_token")

    captured = {}

    def fake_instantiate(module_path, class_name, **kwargs):
        captured.update(kwargs)
        return MagicMock()

    with patch("providers.factory._instantiate", side_effect=fake_instantiate):
        from providers.factory import get_provider

        get_provider(
            "github-models",
            phase="qa",
            model="github-models/openai/gpt-4.1",
        )

    assert captured["base_url"] == "https://models.github.ai/inference"
    assert captured["api_key"] == "ghp_test_token"
    # Prefix should be stripped before passing to the provider
    assert captured["model"] == "openai/gpt-4.1"


def test_get_provider_github_models_uses_gh_token_fallback(monkeypatch):
    """Falls back to GH_TOKEN when GITHUB_TOKEN is not set."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GH_TOKEN", "ghp_fallback")

    captured = {}

    def fake_instantiate(module_path, class_name, **kwargs):
        captured.update(kwargs)
        return MagicMock()

    with patch("providers.factory._instantiate", side_effect=fake_instantiate):
        from providers.factory import get_provider

        get_provider(
            "gh-models",
            phase="qa",
            model="github-models/meta/llama-3.3-70b",
        )

    assert captured["api_key"] == "ghp_fallback"
    assert captured["model"] == "meta/llama-3.3-70b"


def test_get_provider_github_models_caller_override_respected(monkeypatch):
    """Explicit base_url/api_key from caller must not be overwritten."""
    monkeypatch.setenv("GITHUB_TOKEN", "env_token")

    captured = {}

    def fake_instantiate(module_path, class_name, **kwargs):
        captured.update(kwargs)
        return MagicMock()

    with patch("providers.factory._instantiate", side_effect=fake_instantiate):
        from providers.factory import get_provider

        get_provider(
            "github-models",
            phase="qa",
            model="github-models/openai/gpt-4.1",
            base_url="https://custom.endpoint/inference",
            api_key="caller_key",
        )

    assert captured["base_url"] == "https://custom.endpoint/inference"
    assert captured["api_key"] == "caller_key"


def test_get_provider_github_models_model_without_prefix(monkeypatch):
    """Model string without 'github-models/' prefix passes through unchanged."""
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")

    captured = {}

    def fake_instantiate(module_path, class_name, **kwargs):
        captured.update(kwargs)
        return MagicMock()

    with patch("providers.factory._instantiate", side_effect=fake_instantiate):
        from providers.factory import get_provider

        get_provider(
            "github-models",
            phase="qa",
            model="openai/gpt-4.1",
        )

    # model passes through unchanged (no double-stripping)
    assert captured["model"] == "openai/gpt-4.1"
