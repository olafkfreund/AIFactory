"""Provider bounded-retry + failover decision core (#611 c).

Pure unit tests for the failover chain, the next-provider walk, the
"is this worth failing over for?" classifier (the danger-zone guard), and the
monotonic deadline budget.
"""

from __future__ import annotations

import pytest
from core import provider_failover as pf

# ── chain resolution ─────────────────────────────────────────────────────────


def test_configured_chain_default_when_unset():
    assert pf.configured_chain({}) == pf._DEFAULT_CHAIN


def test_configured_chain_env_override():
    chain = pf.configured_chain(
        {"AIFACTORY_PROVIDER_FAILOVER": "ollama, codex ,claude"}
    )
    assert chain == ("ollama", "codex", "claude")


def test_configured_chain_blank_falls_back():
    assert (
        pf.configured_chain({"AIFACTORY_PROVIDER_FAILOVER": "   "}) == pf._DEFAULT_CHAIN
    )


def test_failover_chain_primary_first_and_deduped():
    # primary already in the default chain → not duplicated, but moved to front.
    chain = pf.failover_chain("codex", {})
    assert chain[0] == "codex"
    assert chain.count("codex") == 1
    assert set(chain) >= {"claude", "codex", "antigravity"}


def test_failover_chain_lowercases_and_prepends_novel_primary():
    chain = pf.failover_chain("Ollama", {})
    assert chain[0] == "ollama"
    assert "claude" in chain


# ── next provider ────────────────────────────────────────────────────────────


def test_next_provider_skips_used():
    nxt = pf.next_provider("claude", used={"claude"}, env={})
    assert nxt == "codex"


def test_next_provider_none_when_exhausted():
    nxt = pf.next_provider("claude", used={"claude", "codex", "antigravity"}, env={})
    assert nxt is None


def test_next_provider_case_insensitive_used():
    assert pf.next_provider("claude", used={"CLAUDE", "Codex"}, env={}) == "antigravity"


# ── should_failover (the danger-zone guard) ──────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "Antigravity CLI subprocess timed out after 300s.",
        "connection reset by peer",
        "subprocess crashed with no output",
        None,
        "",
    ],
)
def test_should_failover_true_for_infra(text):
    assert pf.should_failover(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "HTTP 401 Unauthorized",
        "invalid api key",
        "your credential has expired",
        "model not found: claude-opus-999",
        "insufficient_quota",
        "403 permission denied",
    ],
)
def test_should_failover_false_for_config_faults(text):
    assert pf.should_failover(text) is False


# ── deadline budget ──────────────────────────────────────────────────────────


def test_deadline_not_expired_initially():
    b = pf.DeadlineBudget(total_seconds=100)
    assert not b.expired()
    assert 0 < b.remaining() <= 100


def test_deadline_expired_when_zero():
    b = pf.DeadlineBudget(total_seconds=0)
    assert b.expired()
    assert b.remaining() == 0.0


def test_deadline_env_parse_and_fallback():
    assert (
        pf.DeadlineBudget(env={"AIFACTORY_PROVIDER_DEADLINE_S": "42"}).total_seconds
        == 42.0
    )
    # invalid → default
    assert (
        pf.DeadlineBudget(env={"AIFACTORY_PROVIDER_DEADLINE_S": "abc"}).total_seconds
        == pf._DEFAULT_DEADLINE_S
    )
