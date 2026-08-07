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
    _reset()
    yield
    _reset()


def _reset():
    tracing_bootstrap._initialized = False
    tracing_bootstrap._attach_token = None
    tracing_bootstrap._job_span = None
    tracing_bootstrap._provider = None


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


# ── Factory#607: the Job must EMIT, not just inherit ──────────────────────────
#
# This module used to attach the parent context and install no exporter, so a
# PARR trace covered the control plane and stopped at the Job boundary — the
# boundary the work is on the far side of. The tests above prove the trace_id is
# inherited; on their own they passed for the entire time no job-side span had
# ever reached the collector. These prove the other half.


def test_a_span_is_actually_opened_and_parented(monkeypatch):
    """Inheriting a trace_id is not emitting. There must be a real span, and it
    must be a CHILD of the dispatcher's — a root span carrying the same
    trace_id would look right in the collector and be parented to nothing."""
    monkeypatch.setenv("TRACEPARENT", SAMPLE_TP)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    tracing_bootstrap.init_agent_tracing()

    from opentelemetry import trace

    span = trace.get_current_span()
    assert span is tracing_bootstrap._job_span
    assert span.is_recording()
    ctx = span.get_span_context()
    assert ctx.trace_id == int("4bf92f3577b34da6a3ce929d0e0e4736", 16)
    assert span.parent is not None, "the job span must have a parent"
    assert span.parent.span_id == int("00f067aa0ba902b7", 16)


def test_no_traceparent_opens_no_span(monkeypatch):
    """No parent means no trace to join, and an unparented job span is span
    volume with no question attached (Factory#607 scope note 4)."""
    monkeypatch.delenv("TRACEPARENT", raising=False)
    tracing_bootstrap.init_agent_tracing()
    assert tracing_bootstrap._job_span is None


def test_span_carries_the_scalars_that_lead_back_to_the_run(monkeypatch):
    monkeypatch.setenv("TRACEPARENT", SAMPLE_TP)
    monkeypatch.setenv("FACTORY_SERVICE", "aifactory")
    monkeypatch.setenv("JOB_ID", "proj-abc:042-go-hello")
    monkeypatch.setenv("CORRELATION_KEY", "482")
    tracing_bootstrap.init_agent_tracing()

    span = tracing_bootstrap._job_span
    assert span.name == "aifactory job"
    assert span.attributes["factory.job_id"] == "proj-abc:042-go-hello"
    assert span.attributes["factory.correlation_key"] == "482"


def test_exit_flush_is_bounded_and_does_not_shut_down(monkeypatch):
    """A build must not be held open by a collector that is down.

    ``force_flush``'s timeout does not cancel an export already in flight, and
    ``TracerProvider.shutdown()`` flushes AGAIN on the SDK's own 30s budget.
    Measured in-pod against a black hole: 20.54s with shutdown() and the
    exporter default, 4.52s without. So: bounded flush, and no shutdown.
    """
    calls = {}

    class _StubProvider:
        def force_flush(self, timeout_millis):
            calls["flush"] = timeout_millis

        def shutdown(self):
            calls["shutdown"] = True

    class _StubSpan:
        ended = False

        def end(self):
            _StubSpan.ended = True

    monkeypatch.setattr(tracing_bootstrap, "_provider", _StubProvider())
    monkeypatch.setattr(tracing_bootstrap, "_job_span", _StubSpan())
    tracing_bootstrap._flush_at_exit()

    assert _StubSpan.ended, "the job span must be ended before the flush"
    assert calls["flush"] == tracing_bootstrap._FLUSH_TIMEOUT_MS
    assert calls["flush"] <= 5000, "an exiting Job must not wait on a dead collector"
    assert "shutdown" not in calls, "shutdown() would flush again on a 30s budget"
    assert tracing_bootstrap._EXPORT_TIMEOUT_SECONDS <= 3.0


def test_exporter_is_installed_when_an_endpoint_is_configured(monkeypatch):
    """The exporter is the whole fix: without it the span is built and dropped,
    which is exactly the behaviour Factory#607 was filed against."""
    added = []

    class _StubProvider:
        def add_span_processor(self, processor):
            added.append(processor)

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://observe:5080/api/default")
    tracing_bootstrap._install_exporter(_StubProvider())
    assert len(added) == 1

    added.clear()
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    tracing_bootstrap._install_exporter(_StubProvider())
    assert added == [], "no endpoint must mean no exporter, not a broken one"
