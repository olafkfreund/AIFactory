"""Unit tests for the agent-side OTel bootstrap (Epic #35 #42 PR-2).

The agent subprocess inherits ``TRACEPARENT`` from the web-server's
``make_subprocess_env`` and runs ``init_agent_tracing()`` at startup
to re-attach the parent context. These tests cover both branches
(env set / env unset) and the failure-safe contract.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).parent.parent / "apps" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from core import tracing_bootstrap

# Sample valid W3C traceparent. trace_id = 32-hex, span_id = 16-hex.
SAMPLE_TP = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


@pytest.fixture(autouse=True)
def _reset_module_state():
    """Each test starts with a fresh module guard."""
    tracing_bootstrap._initialized = False
    tracing_bootstrap._attach_token = None
    yield
    tracing_bootstrap._initialized = False
    tracing_bootstrap._attach_token = None


def test_no_traceparent_env_is_noop(monkeypatch):
    """Standalone CLI mode — no env var, no exception, no work."""
    monkeypatch.delenv("TRACEPARENT", raising=False)
    tracing_bootstrap.init_agent_tracing()
    assert tracing_bootstrap._initialized is True
    assert tracing_bootstrap._attach_token is None


def test_empty_traceparent_env_is_noop(monkeypatch):
    """Operator typo: empty string treated as absence."""
    monkeypatch.setenv("TRACEPARENT", "")
    tracing_bootstrap.init_agent_tracing()
    assert tracing_bootstrap._initialized is True
    assert tracing_bootstrap._attach_token is None


def test_valid_traceparent_env_attaches_context(monkeypatch):
    """The happy path: env var present, OTel installed, parent
    context attached."""
    monkeypatch.setenv("TRACEPARENT", SAMPLE_TP)
    tracing_bootstrap.init_agent_tracing()
    assert tracing_bootstrap._initialized is True
    # When successful, attach returns a non-None token (detach handle).
    assert tracing_bootstrap._attach_token is not None

    # Verify trace_id propagated. After attach, the *current context*
    # has a span context whose trace_id matches the env var's.
    from opentelemetry import trace
    current = trace.get_current_span()
    ctx = current.get_span_context()
    assert ctx.is_valid
    expected_trace_id = int("4bf92f3577b34da6a3ce929d0e0e4736", 16)
    assert ctx.trace_id == expected_trace_id


def test_malformed_traceparent_is_safe(monkeypatch, caplog):
    """A garbage TRACEPARENT must not crash the agent. The W3C
    propagator silently extracts INVALID_SPAN_CONTEXT when the format
    is wrong; we should detect that as 'no parent' rather than
    raising."""
    monkeypatch.setenv("TRACEPARENT", "not-a-valid-traceparent")
    # Must NOT raise.
    tracing_bootstrap.init_agent_tracing()
    assert tracing_bootstrap._initialized is True
    # Either: attach succeeded with an invalid-span context (token set)
    # or the propagator returned a context with no span at all.
    # Either way the agent starts cleanly.


def test_init_is_idempotent(monkeypatch):
    """Second call must be a no-op even when state changes between
    calls — e.g. agent re-enters startup logic during a recover."""
    monkeypatch.setenv("TRACEPARENT", SAMPLE_TP)
    tracing_bootstrap.init_agent_tracing()
    token_after_first = tracing_bootstrap._attach_token

    # Clobber the env so a NON-idempotent impl would attach a
    # different context. Token must NOT change.
    monkeypatch.setenv("TRACEPARENT", "")
    tracing_bootstrap.init_agent_tracing()
    assert tracing_bootstrap._attach_token is token_after_first


def test_get_inherited_traceparent_returns_env_value(monkeypatch):
    """Log-enrichment helper for callers that can't use full OTel."""
    monkeypatch.setenv("TRACEPARENT", SAMPLE_TP)
    assert tracing_bootstrap.get_inherited_traceparent() == SAMPLE_TP


def test_get_inherited_traceparent_returns_none_when_unset(monkeypatch):
    monkeypatch.delenv("TRACEPARENT", raising=False)
    assert tracing_bootstrap.get_inherited_traceparent() is None


def test_get_inherited_traceparent_strips_whitespace(monkeypatch):
    """An env-loader might add a trailing newline; canonicalise to
    None when the value is whitespace-only."""
    monkeypatch.setenv("TRACEPARENT", "   ")
    assert tracing_bootstrap.get_inherited_traceparent() is None
