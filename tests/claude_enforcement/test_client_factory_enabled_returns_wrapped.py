"""Tests — factory wraps client when enforcement enabled + org_id given (Epic #35 v1.2 #207).

Covers design §1 / §9:
- AIFACTORY_CLAUDE_ENFORCEMENT_ENABLED=true + org_id -> _EnforcedClaudeSDKClient returned.
- Wrong model in allowlist -> ModelNotAllowedError raised at build time.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for _root in (REPO_ROOT / "apps" / "web-server", REPO_ROOT / "apps" / "backend"):
    if str(_root) not in sys.path:
        sys.path.insert(0, str(_root))


def test_enabled_with_org_id_wraps(monkeypatch):
    monkeypatch.setenv("AIFACTORY_CLAUDE_ENFORCEMENT_ENABLED", "true")
    monkeypatch.delenv("LITELLM_GATEWAY_URL", raising=False)
    from core.enforcement import (
        ClaudeEnforcementContext,
        _EnforcedClaudeSDKClient,
        _NoBudgetProvider,
        build_enforcement_context,
        wrap_client_if_enforced,
    )

    class _FakeClient:
        pass

    ctx = build_enforcement_context(
        org_id="org-y",
        user_id="u1",
        model="claude-opus-4-7",
        allowed_models=["claude-*"],
    )
    assert not ctx._is_noop
    result = wrap_client_if_enforced(_FakeClient(), ctx)
    assert isinstance(result, _EnforcedClaudeSDKClient)


def test_model_not_allowed_raises_at_enforce_allowlist(monkeypatch):
    monkeypatch.setenv("AIFACTORY_CLAUDE_ENFORCEMENT_ENABLED", "true")
    monkeypatch.delenv("LITELLM_GATEWAY_URL", raising=False)
    from core.enforcement import build_enforcement_context
    from providers.openai_compatible import ModelNotAllowedError

    ctx = build_enforcement_context(
        org_id="org-y",
        user_id=None,
        model="gpt-4o",
        allowed_models=["claude-*"],
    )
    with pytest.raises(ModelNotAllowedError):
        ctx.enforce_allowlist()


def test_failure_mode_env_defaults_to_open(monkeypatch):
    monkeypatch.setenv("AIFACTORY_CLAUDE_ENFORCEMENT_ENABLED", "true")
    monkeypatch.delenv("AIFACTORY_CLAUDE_ENFORCEMENT_FAILURE_MODE", raising=False)
    monkeypatch.delenv("LITELLM_GATEWAY_URL", raising=False)
    from core.enforcement import build_enforcement_context

    ctx = build_enforcement_context(
        org_id="org-y",
        user_id=None,
        model="claude-opus-4-7",
        allowed_models=["*"],
    )
    assert ctx._failure_mode == "open"


def test_failure_mode_env_closed(monkeypatch):
    monkeypatch.setenv("AIFACTORY_CLAUDE_ENFORCEMENT_ENABLED", "true")
    monkeypatch.setenv("AIFACTORY_CLAUDE_ENFORCEMENT_FAILURE_MODE", "closed")
    monkeypatch.delenv("LITELLM_GATEWAY_URL", raising=False)
    from core.enforcement import build_enforcement_context

    ctx = build_enforcement_context(
        org_id="org-y",
        user_id=None,
        model="claude-opus-4-7",
        allowed_models=["*"],
    )
    assert ctx._failure_mode == "closed"
