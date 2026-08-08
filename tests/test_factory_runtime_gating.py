"""Tests for RFC-0014 §6 runtime gating in the provider factory (#663).

Covers ``runtime_to_provider`` mapping and ``get_runtime_provider`` gating:
claude is always buildable; an un-enabled contract runtime raises rather than
silently falling back; an allowlisted runtime resolves to its provider. Provider
construction is stubbed so the tests do not require any CLI on PATH.
"""

from __future__ import annotations

from typing import Any

import core.runtime_gating as rg
import pytest
from providers import factory


def test_runtime_to_provider_mapping() -> None:
    assert factory.runtime_to_provider("claude") == "claude"
    assert factory.runtime_to_provider("codex") == "codex"
    # The two Ollama runtimes are DISTINCT canonicals since #1213. This line
    # asserted `== "ollama"`, which is the defect written down: both runtimes
    # collapsed to one provider, so both read the same OLLAMA_BASE_URL and
    # `ollama-cloud` was a gating label rather than a routing decision.
    assert factory.runtime_to_provider("ollama") == "ollama"
    assert factory.runtime_to_provider("ollama-cloud") == "ollama-cloud"
    # Speed-up runtimes are orchestration modes over claude.
    assert factory.runtime_to_provider("claude-subagents") == "claude"
    assert factory.runtime_to_provider("dynamic-workflow") == "claude"
    # Unknown passes through unchanged.
    assert factory.runtime_to_provider("future-runtime") == "future-runtime"


def test_get_runtime_provider_default_claude(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def _stub(provider_name: str, phase: str, **kwargs: Any) -> str:
        captured["provider"] = provider_name
        captured["phase"] = phase
        captured["kwargs"] = kwargs
        return "stub-provider"

    monkeypatch.setattr(factory, "get_provider", _stub)
    result = factory.get_runtime_provider(None, phase="coding", env={})
    assert result == "stub-provider"
    assert captured["provider"] == "claude"
    assert captured["phase"] == "coding"


def test_get_runtime_provider_gated_runtime_resolves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def _stub(provider_name: str, phase: str, **kwargs: Any) -> str:
        captured["provider"] = provider_name
        captured["phase"] = phase
        captured["kwargs"] = kwargs
        return "stub-provider"

    monkeypatch.setattr(factory, "get_provider", _stub)
    execution = {"runtime": "codex"}
    env = {rg.ALLOWLIST_ENV: "codex"}
    result = factory.get_runtime_provider(execution, phase="coding", env=env)
    assert result == "stub-provider"
    assert captured["provider"] == "codex"


def test_get_runtime_provider_refuses_ungated(monkeypatch: pytest.MonkeyPatch) -> None:
    # Must raise, NOT silently fall back to claude.
    def _stub(*_args: Any, **_kwargs: Any) -> str:  # pragma: no cover
        raise AssertionError("get_provider must not be called for an ungated runtime")

    monkeypatch.setattr(factory, "get_provider", _stub)
    with pytest.raises(rg.RuntimeNotEnabledError):
        factory.get_runtime_provider({"runtime": "codex"}, phase="coding", env={})
