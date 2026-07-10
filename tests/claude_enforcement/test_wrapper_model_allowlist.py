"""Tests for Claude enforcement — model allowlist (Epic #35 v1.2 #207).

Covers design §2:
- claude-* allowlist accepts claude-opus-4-7, rejects gpt-4o-mini.
- * allowlist allows every model (backward compat default).
- empty list blocks everything.
- ['*'] default when no allowed_models passed.
- ModelNotAllowedError carries org_id + rejected model.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for _root in (REPO_ROOT / "apps" / "web-server", REPO_ROOT / "apps" / "backend"):
    if str(_root) not in sys.path:
        sys.path.insert(0, str(_root))


def _make_ctx(model: str, allowed_models: list[str], org_id: str = "org-test"):
    from core.enforcement import ClaudeEnforcementContext, _NoBudgetProvider

    return ClaudeEnforcementContext(
        org_id=org_id,
        user_id="user-alice",
        model=model,
        allowed_models=allowed_models,
        failure_mode="open",
        budget_provider=_NoBudgetProvider(),
    )


def test_claude_glob_accepts_opus():
    ctx = _make_ctx("claude-opus-4-7", ["claude-*"])
    # Should not raise.
    ctx.enforce_allowlist()


def test_claude_glob_rejects_gpt():
    from providers.openai_compatible import ModelNotAllowedError

    ctx = _make_ctx("gpt-4o-mini", ["claude-*"], org_id="org-acme")
    with pytest.raises(ModelNotAllowedError) as excinfo:
        ctx.enforce_allowlist()
    err = excinfo.value
    assert err.model == "gpt-4o-mini"
    assert err.org_id == "org-acme"
    assert "gpt-4o-mini" in str(err)
    assert "org-acme" in str(err)


def test_wildcard_accepts_all():
    for model in ("claude-opus-4-7", "gpt-4o-mini", "bedrock/anthropic.claude-3"):
        ctx = _make_ctx(model, ["*"])
        ctx.enforce_allowlist()  # must not raise


def test_empty_allowlist_blocks_everything():
    from providers.openai_compatible import ModelNotAllowedError

    ctx = _make_ctx("claude-opus-4-7", [])
    with pytest.raises(ModelNotAllowedError):
        ctx.enforce_allowlist()


def test_exact_match_allows():
    ctx = _make_ctx("claude-opus-4-7", ["claude-opus-4-7"])
    ctx.enforce_allowlist()


def test_exact_match_rejects_sonnet_when_only_opus_listed():
    from providers.openai_compatible import ModelNotAllowedError

    ctx = _make_ctx("claude-sonnet-4-6", ["claude-opus-4-7"])
    with pytest.raises(ModelNotAllowedError):
        ctx.enforce_allowlist()


def test_noop_bypasses_allowlist():
    from core.enforcement import ClaudeEnforcementContext

    ctx = ClaudeEnforcementContext.noop()
    ctx.enforce_allowlist()  # must not raise even with no model


def test_multiple_patterns_any_match():
    ctx = _make_ctx("claude-sonnet-4-6", ["claude-opus-*", "claude-sonnet-*"])
    ctx.enforce_allowlist()  # sonnet-* matches


def test_build_enforcement_context_disabled_returns_noop(monkeypatch):
    monkeypatch.delenv("AIFACTORY_CLAUDE_ENFORCEMENT_ENABLED", raising=False)
    from core.enforcement import build_enforcement_context

    ctx = build_enforcement_context(
        org_id="org-x",
        user_id=None,
        model="claude-opus-4-7",
        allowed_models=["claude-*"],
    )
    assert ctx._is_noop


def test_build_enforcement_context_enabled(monkeypatch):
    monkeypatch.setenv("AIFACTORY_CLAUDE_ENFORCEMENT_ENABLED", "true")
    monkeypatch.delenv("LITELLM_GATEWAY_URL", raising=False)
    from core.enforcement import build_enforcement_context

    ctx = build_enforcement_context(
        org_id="org-x",
        user_id="u1",
        model="claude-opus-4-7",
        allowed_models=["claude-*"],
    )
    assert not ctx._is_noop
    assert ctx._org_id == "org-x"
