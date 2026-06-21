"""Tests for RFC-0014 §6 gated-runtime registry (#663).

Covers the operator allowlist (``AIFACTORY_RUNTIMES``), the always-on ``claude``
default, the ``all`` token that intentionally EXCLUDES the manual-only speed-up
runtimes, contract opt-in parsing, and the no-silent-fallback refusal.
"""

from __future__ import annotations

import core.runtime_gating as rg
import pytest


def test_claude_always_enabled_without_allowlist() -> None:
    assert rg.is_runtime_enabled("claude", {}) is True
    assert rg.is_runtime_enabled(None, {}) is True
    assert rg.operator_allowlist({}) == frozenset({"claude"})


def test_non_claude_off_without_allowlist() -> None:
    for runtime in ("codex", "antigravity", "ollama", "ollama-cloud"):
        assert rg.is_runtime_enabled(runtime, {}) is False


def test_allowlist_enables_named_runtimes() -> None:
    env = {rg.ALLOWLIST_ENV: "codex, ollama"}
    assert rg.is_runtime_enabled("codex", env) is True
    assert rg.is_runtime_enabled("ollama", env) is True
    assert rg.is_runtime_enabled("antigravity", env) is False


def test_unknown_tokens_ignored() -> None:
    env = {rg.ALLOWLIST_ENV: "definitely-not-a-runtime, codex"}
    assert rg.operator_allowlist(env) == frozenset({"claude", "codex"})


def test_all_token_excludes_speed_up_runtimes() -> None:
    env = {rg.ALLOWLIST_ENV: "all"}
    assert rg.is_runtime_enabled("codex", env) is True
    assert rg.is_runtime_enabled("antigravity", env) is True
    assert rg.is_runtime_enabled("ollama-cloud", env) is True
    # The speed-up runtimes multiply spend and are manual-enable only.
    assert rg.is_runtime_enabled("claude-subagents", env) is False
    assert rg.is_runtime_enabled("dynamic-workflow", env) is False


def test_speed_up_runtimes_require_explicit_enable() -> None:
    env = {rg.ALLOWLIST_ENV: "claude-subagents"}
    assert rg.is_runtime_enabled("claude-subagents", env) is True
    assert rg.is_runtime_enabled("dynamic-workflow", env) is False


def test_runtime_from_execution() -> None:
    assert rg.runtime_from_execution({"runtime": "codex"}) == "codex"
    assert rg.runtime_from_execution({"runtime": "  CODEX "}) == "codex"
    assert rg.runtime_from_execution({}) == "claude"
    assert rg.runtime_from_execution(None) == "claude"
    # Non-string values resolve to the default.
    assert rg.runtime_from_execution({"runtime": 123}) == "claude"


def test_resolve_runtime_opted_in_and_enabled() -> None:
    env = {rg.ALLOWLIST_ENV: "codex"}
    assert rg.resolve_runtime({"runtime": "codex"}, env) == "codex"
    assert rg.resolve_runtime({"runtime": "claude"}, {}) == "claude"
    assert rg.resolve_runtime(None, {}) == "claude"


def test_resolve_runtime_raises_not_silent_fallback() -> None:
    with pytest.raises(rg.RuntimeNotEnabledError) as exc:
        rg.resolve_runtime({"runtime": "codex"}, {})
    assert exc.value.runtime == "codex"
    assert "claude" in exc.value.enabled


def test_resolve_runtime_speed_up_error_hints_explicit_enable() -> None:
    # 'all' does not cover the speed-up runtimes; the error must say so.
    env = {rg.ALLOWLIST_ENV: "all"}
    with pytest.raises(rg.RuntimeNotEnabledError) as exc:
        rg.resolve_runtime({"runtime": "dynamic-workflow"}, env)
    assert "explicitly" in str(exc.value)


def test_selectable_runtimes_report() -> None:
    report = rg.selectable_runtimes({rg.ALLOWLIST_ENV: "codex"})
    assert report["claude"] is True
    assert report["codex"] is True
    assert report["ollama"] is False
    assert set(report) == set(rg.KNOWN_RUNTIMES)


def test_module_self_tests_pass() -> None:
    # The module's embedded self-tests are part of its contract.
    rg._test()
