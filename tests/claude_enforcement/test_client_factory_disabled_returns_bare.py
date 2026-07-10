"""Tests — factory returns bare client when enforcement disabled (Epic #35 v1.2 #207).

Covers design §9 backward compat:
- AIFACTORY_CLAUDE_ENFORCEMENT_ENABLED unset -> noop enforcement context.
- No org_id -> noop enforcement context.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
for _root in (REPO_ROOT / "apps" / "web-server", REPO_ROOT / "apps" / "backend"):
    if str(_root) not in sys.path:
        sys.path.insert(0, str(_root))


def test_env_unset_returns_noop(monkeypatch):
    monkeypatch.delenv("AIFACTORY_CLAUDE_ENFORCEMENT_ENABLED", raising=False)
    from core.enforcement import build_enforcement_context

    ctx = build_enforcement_context(
        org_id="org-x",
        user_id=None,
        model="claude-opus-4-7",
        allowed_models=["*"],
    )
    assert ctx._is_noop


def test_env_false_returns_noop(monkeypatch):
    monkeypatch.setenv("AIFACTORY_CLAUDE_ENFORCEMENT_ENABLED", "false")
    from core.enforcement import build_enforcement_context

    ctx = build_enforcement_context(
        org_id="org-x",
        user_id=None,
        model="claude-opus-4-7",
        allowed_models=["*"],
    )
    assert ctx._is_noop


def test_no_org_id_returns_noop(monkeypatch):
    monkeypatch.setenv("AIFACTORY_CLAUDE_ENFORCEMENT_ENABLED", "true")
    from core.enforcement import build_enforcement_context

    ctx = build_enforcement_context(
        org_id=None,
        user_id=None,
        model="claude-opus-4-7",
        allowed_models=["*"],
    )
    assert ctx._is_noop


def test_wrap_client_if_enforced_with_noop_returns_bare():
    from core.enforcement import ClaudeEnforcementContext, wrap_client_if_enforced

    class _FakeClient:
        pass

    noop = ClaudeEnforcementContext.noop()
    result = wrap_client_if_enforced(_FakeClient(), noop)
    assert type(result).__name__ == "_FakeClient"


def test_wrap_client_if_enforced_with_active_wraps():
    from core.enforcement import (
        ClaudeEnforcementContext,
        _EnforcedClaudeSDKClient,
        _NoBudgetProvider,
        wrap_client_if_enforced,
    )

    class _FakeClient:
        pass

    ctx = ClaudeEnforcementContext(
        org_id="org-x",
        user_id=None,
        model="claude-opus-4-7",
        allowed_models=["*"],
        budget_provider=_NoBudgetProvider(),
    )
    result = wrap_client_if_enforced(_FakeClient(), ctx)
    assert isinstance(result, _EnforcedClaudeSDKClient)
