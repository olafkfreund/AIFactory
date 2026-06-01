"""Tests — no org_id returns bare client regardless of enforcement flag (Epic #35 v1.2 #207).

Covers design §9 / §10: system-task escape hatch.
CLI / runners that call create_client() without org_id stay v1.1 behaviour.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
for _root in (REPO_ROOT / 'apps' / 'web-server', REPO_ROOT / 'apps' / 'backend'):
    if str(_root) not in sys.path:
        sys.path.insert(0, str(_root))


def test_no_org_id_returns_noop_even_when_enforcement_enabled(monkeypatch):
    monkeypatch.setenv('AIFACTORY_CLAUDE_ENFORCEMENT_ENABLED', 'true')
    monkeypatch.delenv('LITELLM_GATEWAY_URL', raising=False)
    from core.enforcement import build_enforcement_context

    ctx = build_enforcement_context(
        org_id=None,      # system task — no org context
        user_id=None,
        model='claude-opus-4-7',
        allowed_models=['claude-*'],
    )
    assert ctx._is_noop, (
        'create_client() callers without org_id must receive noop enforcement '
        'regardless of AIFACTORY_CLAUDE_ENFORCEMENT_ENABLED.  CLI / spec_runner '
        'invocations must remain v1.1 compatible.'
    )


def test_noop_context_allows_any_model():
    from core.enforcement import ClaudeEnforcementContext

    noop = ClaudeEnforcementContext.noop()
    # enforce_allowlist() on noop must never raise, even for non-Claude models.
    noop.enforce_allowlist()
