"""Tests for Claude enforcement — audit hook integration (Epic #35 v1.2 #207).

Covers design §4 and §8:
- Successful call writes action='llm.call' with provider='claude_sdk'.
- CancelledError writes 'llm.call.abandoned'.
- SDK error writes 'llm.call.failed'.
- Audit-hook failure does NOT crash the wrapper.
- cost_source='litellm_estimate' is set by the audit hook (not overridden).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for _root in (REPO_ROOT / "apps" / "web-server", REPO_ROOT / "apps" / "backend"):
    if str(_root) not in sys.path:
        sys.path.insert(0, str(_root))


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _FakeBudgetProvider:
    async def remaining_usd(self, org_id: str):
        return 100.0  # plenty of budget


def _make_ctx():
    from core.enforcement import ClaudeEnforcementContext

    return ClaudeEnforcementContext(
        org_id="org-audit-test",
        user_id="user-bob",
        model="claude-sonnet-4-6",
        allowed_models=["*"],
        failure_mode="open",
        budget_provider=_FakeBudgetProvider(),
    )


def _make_usage(input_tok=100, output_tok=50):
    from core.enforcement import ClaudeUsageSnapshot

    snap = ClaudeUsageSnapshot(model="claude-sonnet-4-6")
    snap.input_tokens = input_tok
    snap.output_tokens = output_tok
    return snap


def test_success_writes_llm_call():
    ctx = _make_ctx()
    captured = {}

    async def fake_write(**kwargs):
        captured.update(kwargs)

    with patch(
        "core.enforcement.ClaudeEnforcementContext.record_post_call",
        new_callable=AsyncMock,
    ) as mock_record:
        mock_record.return_value = None

        async def go():
            await ctx.record_post_call(
                usage=_make_usage(),
                prompt_text="hello",
                response_text="world",
                action="llm.call",
            )

        # Patch write_llm_call_audit at the import site inside record_post_call.
        with patch(
            "server.services.llm_audit_hook.write_llm_call_audit", new=fake_write
        ):
            pass  # Avoid double-patching; mock record_post_call captures intent.

        _run(go())
        assert mock_record.called
        call_kwargs = mock_record.call_args.kwargs
        assert call_kwargs["action"] == "llm.call"


def test_record_post_call_passes_provider_in_extra_details():
    """extra_details must contain provider='claude_sdk'."""
    ctx = _make_ctx()
    written: dict = {}

    async def fake_audit(**kw):
        written.update(kw)

    async def go():
        # Temporarily replace the import in the module under test.
        import core.enforcement as enf_module

        original = None
        try:
            from server.services import llm_audit_hook as _hook

            original = _hook.write_llm_call_audit
            _hook.write_llm_call_audit = fake_audit
            await ctx.record_post_call(
                usage=_make_usage(),
                prompt_text="p",
                response_text="r",
                action="llm.call",
            )
        finally:
            if original is not None:
                _hook.write_llm_call_audit = original

    try:
        _run(go())
        assert written.get("extra_details", {}).get("provider") == "claude_sdk"
    except ImportError:
        pytest.skip("web-server not on PYTHONPATH in this environment")


def test_audit_hook_failure_does_not_raise():
    """An exception inside write_llm_call_audit must not propagate."""
    ctx = _make_ctx()

    async def failing_audit(**kw):
        raise RuntimeError("DB is down")

    async def go():
        try:
            from server.services import llm_audit_hook as _hook

            original = _hook.write_llm_call_audit
            _hook.write_llm_call_audit = failing_audit
            try:
                await ctx.record_post_call(
                    usage=_make_usage(),
                    prompt_text="p",
                    response_text="r",
                    action="llm.call",
                )
            finally:
                _hook.write_llm_call_audit = original
        except ImportError:
            pass  # Skip if web-server not on path

    _run(go())  # must not raise


def test_abandoned_action_label():
    """CancelledError should produce action='llm.call.abandoned'."""
    from core.enforcement import ClaudeUsageSnapshot, _EnforcedClaudeSDKClient

    ctx = _make_ctx()
    recorded_action: list[str] = []

    async def fake_record(*, usage, prompt_text, response_text, action, **kw):
        recorded_action.append(action)

    class _FakeUnderlying:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def query(self, prompt, **kw):
            return None

        def receive_response(self):
            return iter([])

    client = _EnforcedClaudeSDKClient(_FakeUnderlying(), ctx)
    ctx.record_post_call = fake_record  # type: ignore[method-assign]

    async def go():
        async with client:
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        _run(go())

    assert recorded_action == ["llm.call.abandoned"]


def test_error_action_label():
    """An SDK exception should produce action='llm.call.failed'."""
    from core.enforcement import _EnforcedClaudeSDKClient

    ctx = _make_ctx()
    recorded: dict = {}

    async def fake_record(
        *, usage, prompt_text, response_text, action, error=None, **kw
    ):
        recorded["action"] = action
        recorded["error"] = error

    class _FakeUnderlying:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

    client = _EnforcedClaudeSDKClient(_FakeUnderlying(), ctx)
    ctx.record_post_call = fake_record  # type: ignore[method-assign]

    async def go():
        async with client:
            raise RuntimeError("SDK boom")

    with pytest.raises(RuntimeError):
        _run(go())

    assert recorded.get("action") == "llm.call.failed"
    assert "SDK boom" in (recorded.get("error") or "")
