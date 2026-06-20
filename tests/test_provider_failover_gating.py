"""Tests for RFC-0014 §6 gating of the provider failover chain (#663).

The failover chain must never reach for a runtime the operator has not enabled.
These tests cover ``enabled_failover_chain`` and the new ``gated=`` flag on
``next_provider`` (default False preserves the pre-RFC-0014 behaviour).
"""

from __future__ import annotations

import core.provider_failover as pf
import core.runtime_gating as rg


def test_enabled_chain_filters_to_allowlist() -> None:
    # No allowlist -> only claude is enabled; codex/antigravity drop out, but the
    # primary is always kept (gating is enforced at selection, not here).
    chain = pf.enabled_failover_chain("claude", env={})
    assert chain == ("claude",)


def test_enabled_chain_keeps_allowlisted_runtimes() -> None:
    env = {rg.ALLOWLIST_ENV: "codex"}
    chain = pf.enabled_failover_chain("claude", env=env)
    assert "claude" in chain
    assert "codex" in chain
    assert "antigravity" not in chain


def test_enabled_chain_keeps_primary_even_if_not_allowlisted() -> None:
    # The operator deliberately chose the primary; keep it so the chain is never
    # empty and the deliberate choice is honoured.
    chain = pf.enabled_failover_chain("codex", env={})
    assert chain[0] == "codex"


def test_ungated_next_provider_unchanged() -> None:
    # Default (gated=False): full chain, unfiltered (back-compat).
    nxt = pf.next_provider("claude", used=["claude"], env={})
    assert nxt == "codex"


def test_gated_next_provider_skips_disabled() -> None:
    # gated=True with no allowlist: after claude there is nothing enabled to fail
    # over to, so the chain is exhausted -> None (caller escalates).
    nxt = pf.next_provider("claude", used=["claude"], env={}, gated=True)
    assert nxt is None


def test_gated_next_provider_uses_allowlisted() -> None:
    env = {rg.ALLOWLIST_ENV: "codex"}
    nxt = pf.next_provider("claude", used=["claude"], env=env, gated=True)
    assert nxt == "codex"
