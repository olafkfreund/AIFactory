#!/usr/bin/env python3
"""Tests for the approved-model registry (#323, #310).

Covers scope (Claude-only), stage approval, the fail-safe registry override, and
the three enforcement modes (warn = advisory, deny = raises, off = no-op).
"""

from __future__ import annotations

import pytest
from model_registry import (
    ENV_ENFORCE,
    ENV_REGISTRY,
    ModelNotApprovedError,
    check_model_registered,
    enforce_model_registry,
    load_registry,
)


class TestCheck:
    def test_approved_claude_model_for_stage(self):
        ok, reason = check_model_registered("opus", "coding")
        assert ok, reason

    def test_full_id_also_resolves(self):
        ok, reason = check_model_registered("claude-opus-4-8", "planning")
        assert ok, reason

    def test_registered_but_wrong_stage(self):
        # Haiku is registered for qa only, not for coding.
        ok, reason = check_model_registered("haiku", "coding")
        assert not ok
        assert "not approved for stage" in reason

    def test_unregistered_claude_model(self):
        ok, reason = check_model_registered("claude-opus-9-9", "coding")
        assert not ok
        assert "not in the approved model registry" in reason

    def test_non_claude_model_is_out_of_scope(self):
        # Provider-prefixed local/third-party models are never governed here.
        for model in ("ollama:qwen3:14b", "openai:gpt-4o", "codex", "gemini-2.5"):
            ok, _ = check_model_registered(model, "coding")
            assert ok, model


class TestEnforce:
    def test_off_mode_is_noop(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv(ENV_ENFORCE, "off")
        # Would be unregistered, but off skips the check entirely.
        enforce_model_registry("claude-opus-9-9", "coding")

    def test_warn_mode_never_raises(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv(ENV_ENFORCE, "warn")
        enforce_model_registry("claude-opus-9-9", "coding")  # logs, no raise

    def test_default_mode_is_warn(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv(ENV_ENFORCE, raising=False)
        enforce_model_registry("claude-opus-9-9", "coding")  # no raise

    def test_deny_mode_raises_on_unregistered(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv(ENV_ENFORCE, "deny")
        with pytest.raises(ModelNotApprovedError):
            enforce_model_registry("claude-opus-9-9", "coding")

    def test_deny_mode_allows_approved(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv(ENV_ENFORCE, "deny")
        enforce_model_registry("opus", "coding")  # approved → no raise


class TestRegistryOverride:
    def test_invalid_json_falls_back_to_default(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv(ENV_REGISTRY, "{not json")
        assert "claude-opus-4-8" in load_registry()

    def test_inline_json_override(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv(
            ENV_REGISTRY,
            '{"claude-opus-4-8": {"provenance": "Anthropic", "stages": ["qa"]}}',
        )
        ok, reason = check_model_registered("opus", "coding")
        assert not ok  # override narrows opus to qa only
        assert "not approved for stage" in reason


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
