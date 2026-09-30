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


def test_unknown_tokens_raise_and_name_themselves() -> None:
    """#1607 reversed this: it used to assert unknown tokens were ignored.

    Dropping them meant a typo silently disabled what the operator believed they
    had enabled. The good token in the same value is deliberately not enough to
    make the call succeed — a partially-applied allowlist is the ambiguous state
    worth refusing.
    """
    env = {rg.ALLOWLIST_ENV: "definitely-not-a-runtime, codex"}
    with pytest.raises(ValueError, match="definitely-not-a-runtime"):
        rg.operator_allowlist(env)


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


# --------------------------------------------------------------------------- #
# #1607: the gate's vocabulary must not drift from the factory's
# --------------------------------------------------------------------------- #


def _factory_canonicals() -> frozenset[str]:
    """Every provider the live path can actually resolve to."""
    from providers import factory

    return frozenset(factory._AGENTIC_REGISTRY) | frozenset(factory._TEXT_REGISTRY)


def test_every_factory_canonical_is_a_known_runtime() -> None:
    """THE regression test for #1607.

    ``KNOWN_RUNTIMES`` and the factory registries were two hand-maintained lists,
    and they drifted: ``copilot``, ``github-models``, ``openai-compatible`` and
    ``opencode`` were producible by the live path and absent from the gate. Since
    ``operator_allowlist`` drops unrecognised tokens, enforcing in that state
    would have made all four permanently unreachable — no value an operator could
    set would re-enable them.
    """
    missing = _factory_canonicals() - rg.KNOWN_RUNTIMES
    assert not missing, (
        f"providers the factory can resolve but the gate cannot name: {sorted(missing)}. "
        "Enforcement would make these permanently unreachable."
    )


def test_known_runtimes_adds_only_the_claude_mapped_pair() -> None:
    """The derived set is the factory's vocabulary plus the two runtimes that
    have no factory entry because they map to ``claude``."""
    extra = rg.KNOWN_RUNTIMES - _factory_canonicals()
    assert extra == rg.MANUAL_ENABLE_ONLY


def test_alias_tokens_normalise_to_their_canonical() -> None:
    """``gemini`` is a valid factory alias for ``antigravity`` but was absent
    from ``KNOWN_RUNTIMES``, so an operator enabling it silently got nothing."""
    assert "antigravity" in rg.operator_allowlist({rg.ALLOWLIST_ENV: "gemini"})


def test_unrecognised_token_raises_rather_than_disabling_silently() -> None:
    """Silent drop protected against a typo *enabling* something. The realistic
    error is a typo *disabling* what the operator believes they enabled, which
    surfaces hours later as a build failure pointing nowhere near the variable.
    """
    with pytest.raises(ValueError, match="AIFACTORY_RUNTIMES"):
        rg.operator_allowlist({rg.ALLOWLIST_ENV: "codex,nosuchruntime"})


def test_copilot_is_enableable(monkeypatch: pytest.MonkeyPatch) -> None:
    """#790 reduces to this: the provider, credential and CLI are all present;
    only the registry entry was missing."""
    assert "copilot" in rg.KNOWN_RUNTIMES
    assert rg.is_runtime_enabled("copilot", {rg.ALLOWLIST_ENV: "copilot"}) is True
    assert rg.is_runtime_enabled("copilot", {}) is False
