"""Tests for Claude enforcement — budget pre-check (Epic #35 v1.2 #207).

Covers design §3 and §5:
- Under budget -> call proceeds.
- Budget = 0 -> BudgetExceededError raised pre-call.
- Budget read failure + failure_mode=open -> log + proceed.
- Budget read failure + failure_mode=closed -> BudgetCheckUnavailableError.
- No virtual key (None remaining) -> unlimited, proceed.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

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
    """Injected fake for budget tests."""

    def __init__(self, remaining: float | None, raises: Exception | None = None):
        self._remaining = remaining
        self._raises = raises

    async def remaining_usd(self, org_id: str) -> float | None:
        if self._raises is not None:
            raise self._raises
        return self._remaining


def _make_ctx(
    remaining: float | None,
    failure_mode: str = "open",
    raises: Exception | None = None,
):
    from core.enforcement import ClaudeEnforcementContext

    ctx = ClaudeEnforcementContext(
        org_id="org-budget-test",
        user_id="user-alice",
        model="claude-opus-4-7",
        allowed_models=["*"],
        failure_mode=failure_mode,
        budget_provider=_FakeBudgetProvider(remaining, raises),
    )
    return ctx


def test_under_budget_proceeds():
    ctx = _make_ctx(remaining=5.0)
    _run(ctx.enforce_budget())  # must not raise


def test_exactly_zero_budget_blocks():
    from core.enforcement import BudgetExceededError

    ctx = _make_ctx(remaining=0.0)
    with pytest.raises(BudgetExceededError) as excinfo:
        _run(ctx.enforce_budget())
    assert "org-budget-test" in str(excinfo.value)


def test_negative_budget_blocks():
    from core.enforcement import BudgetExceededError

    ctx = _make_ctx(remaining=-0.01)
    with pytest.raises(BudgetExceededError):
        _run(ctx.enforce_budget())


def test_none_remaining_unlimited():
    ctx = _make_ctx(remaining=None)
    _run(ctx.enforce_budget())  # None means unlimited


def test_budget_failure_open_mode_proceeds():
    ctx = _make_ctx(
        remaining=None,
        failure_mode="open",
        raises=RuntimeError("LiteLLM down"),
    )
    _run(ctx.enforce_budget())  # must not raise in open mode


def test_budget_failure_closed_mode_raises():
    from core.enforcement import BudgetCheckUnavailableError

    ctx = _make_ctx(
        remaining=None,
        failure_mode="closed",
        raises=RuntimeError("LiteLLM down"),
    )
    with pytest.raises(BudgetCheckUnavailableError) as excinfo:
        _run(ctx.enforce_budget())
    assert "org-budget-test" in str(excinfo.value)
    assert "failure_mode=closed" in str(excinfo.value)


def test_noop_skips_budget_check():
    from core.enforcement import ClaudeEnforcementContext

    ctx = ClaudeEnforcementContext.noop()
    _run(ctx.enforce_budget())  # must not raise


def test_litellm_budget_provider_under_budget():
    """LiteLLMBudgetProvider computes remaining from spend + max_budget."""
    from core.enforcement import LiteLLMBudgetProvider

    class _FakeAdminClient:
        async def get_virtual_key_info(self, org_id):
            return {"max_budget": 10.0, "spend": 3.0}

    provider = LiteLLMBudgetProvider(_FakeAdminClient())
    remaining = _run(provider.remaining_usd("org-x"))
    assert abs(remaining - 7.0) < 1e-9


def test_litellm_budget_provider_no_key_returns_none():
    """No virtual key -> None (unlimited)."""
    from core.enforcement import LiteLLMBudgetProvider

    class _FakeAdminClient:
        async def get_virtual_key_info(self, org_id):
            return None

    provider = LiteLLMBudgetProvider(_FakeAdminClient())
    remaining = _run(provider.remaining_usd("org-no-key"))
    assert remaining is None


def test_litellm_budget_provider_zero_max_budget_unlimited():
    """max_budget=0 or None -> unlimited (opt-out of budget enforcement)."""
    from core.enforcement import LiteLLMBudgetProvider

    class _FakeAdminClient:
        async def get_virtual_key_info(self, org_id):
            return {"max_budget": 0, "spend": 0}

    provider = LiteLLMBudgetProvider(_FakeAdminClient())
    remaining = _run(provider.remaining_usd("org-x"))
    assert remaining is None
