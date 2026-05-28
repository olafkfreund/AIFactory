"""Unit tests for OpenTelemetry distributed tracing (Epic #35 #42 PR-1).

All tests use ``InMemorySpanExporter`` + ``SimpleSpanProcessor`` so
no network is needed and assertions don't race the async batch
flush. Per the design doc's testing fixture note.

Covers:
- ``init_tracing`` is idempotent and tolerates a pre-installed
  TracerProvider (so a test fixture can pre-install one)
- ``get_current_traceparent`` returns a valid W3C string inside a
  span, None outside
- ``task_phase_span`` creates a child span with the right attributes
  and gets nested under a parent span when present
- ``CorrelationIdMiddleware`` sources request_id from trace_id when
  a span is active; falls back to header / UUID otherwise
- ``make_subprocess_env`` injects ``TRACEPARENT`` when called in a
  span; omits it when not
- ``event_bus`` envelope carries ``trace.traceparent`` when published
  inside a span; tolerates absence on receive (older envelopes)
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

_WEB_SERVER = Path(__file__).parent.parent / "apps" / "web-server"
if str(_WEB_SERVER) not in sys.path:
    sys.path.insert(0, str(_WEB_SERVER))


from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

# Import the modules under test AFTER setting up sys.path.
from server.observability import tracing as tracing_module
from server.observability.tracing import (
    get_current_traceparent,
    init_tracing,
    task_phase_span,
)

# ---------------------------------------------------------------------------
# Fixtures
#
# OTel only permits a single TracerProvider per process. Install
# one at session scope with an InMemorySpanExporter + SimpleSpanProcessor
# (SimpleSpanProcessor exports synchronously so assertions don't race
# the async batch flush). Per-test, just clear the exporter.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session", autouse=True)
def _session_tracer_provider():
    """Wire an InMemorySpanExporter onto the active TracerProvider.

    OTel only allows ``set_tracer_provider`` once per process — and
    other test modules' imports may already have wired one up before
    we run. So instead of fighting that, we adapt:

    - If a TracerProvider is already installed, just add our
      SpanProcessor to it.
    - If no SDK TracerProvider is present yet (still the default
      proxy), install one of our own.

    Either way, our InMemorySpanExporter ends up receiving spans for
    the test session."""
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)

    current = trace.get_tracer_provider()
    if isinstance(current, TracerProvider):
        # Pre-existing SDK provider — attach our processor to it.
        current.add_span_processor(processor)
        provider = current
    else:
        # Default proxy provider — install ours.
        provider = TracerProvider(
            resource=Resource.create({"service.name": "test"})
        )
        provider.add_span_processor(processor)
        trace.set_tracer_provider(provider)

    # Prevent init_tracing()'s lifespan call from clobbering us.
    tracing_module._tracer_provider = provider
    tracing_module._initialized = True

    yield exporter


@pytest.fixture
def in_memory_tracing(_session_tracer_provider):
    """Per-test wrapper: clear span buffer + yield the session exporter."""
    _session_tracer_provider.clear()
    yield _session_tracer_provider


# ---------------------------------------------------------------------------
# init_tracing
# ---------------------------------------------------------------------------


def test_init_tracing_is_idempotent(in_memory_tracing, monkeypatch):
    """Calling init_tracing twice doesn't double-install or crash.
    Matters because the lifespan can re-fire in tests / hot reload."""
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    # Reset guard so init_tracing does real work; exercises the
    # detection-of-preinstalled-provider branch.
    tracing_module._initialized = False
    init_tracing()
    init_tracing()  # second call must be a no-op
    # Confirm the session provider is still the one in place — i.e.
    # we didn't clobber it.
    assert trace.get_tracer_provider() is tracing_module._tracer_provider


def test_init_tracing_respects_preinstalled_provider(in_memory_tracing):
    """When a test fixture has pre-installed a TracerProvider, init
    must NOT clobber it (otherwise our InMemorySpanExporter would
    get replaced with a real OTLPSpanExporter or no-op provider)."""
    init_tracing()
    # The fixture's provider should still be in place. Easiest proof:
    # creating + exporting a span lands in the fixture's exporter.
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("probe-span"):
        pass
    spans = in_memory_tracing.get_finished_spans()
    assert any(s.name == "probe-span" for s in spans)


# ---------------------------------------------------------------------------
# get_current_traceparent
# ---------------------------------------------------------------------------


def test_traceparent_format_inside_span(in_memory_tracing):
    """Inside an active span, returns a W3C-format traceparent string."""
    init_tracing()
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("probe"):
        tp = get_current_traceparent()
        assert tp is not None
        # Format: 00-{32 hex}-{16 hex}-{2 hex}
        assert re.match(r"^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$", tp)


def test_traceparent_none_outside_span():
    """Outside any span, returns None (caller's fallback path)."""
    # Don't use the fixture: we explicitly want NO active span. After
    # other tests have run, an OTel proxy provider may still be
    # attached but no span context is active here.
    tp = get_current_traceparent()
    assert tp is None


# ---------------------------------------------------------------------------
# task_phase_span
# ---------------------------------------------------------------------------


def test_task_phase_span_creates_named_span_with_attributes(in_memory_tracing):
    """The manual span carries the contract the design promised:
    name ``task:phase:<phase>`` + ``task.id`` + ``task.phase`` attrs."""
    init_tracing()
    with task_phase_span("001-add-auth", "coding"):
        pass
    spans = in_memory_tracing.get_finished_spans()
    matching = [s for s in spans if s.name == "task:phase:coding"]
    assert len(matching) == 1
    span = matching[0]
    assert span.attributes.get("task.id") == "001-add-auth"
    assert span.attributes.get("task.phase") == "coding"


def test_task_phase_span_nests_under_parent(in_memory_tracing):
    """When called inside an existing span, becomes its child. This is
    what lets the agent-task spans show up under the HTTP request
    span in Tempo."""
    init_tracing()
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("parent") as parent:
        with task_phase_span("042-feat", "review_pending") as child_span:
            child_ctx = child_span.get_span_context()
            parent_ctx = parent.get_span_context()
            # Same trace, different span ids.
            assert child_ctx.trace_id == parent_ctx.trace_id
            assert child_ctx.span_id != parent_ctx.span_id


# ---------------------------------------------------------------------------
# CorrelationIdMiddleware — request_id from trace_id when in span
# ---------------------------------------------------------------------------


def test_correlation_id_helper_extracts_trace_id(in_memory_tracing):
    """The middleware's `_try_trace_id` helper returns the active
    span's trace_id as 32-char hex. Direct unit test of the helper
    avoids spinning up a full ASGI stack."""
    init_tracing()
    from server.observability.correlation_id import _try_trace_id
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("probe") as span:
        rid = _try_trace_id()
        assert rid is not None
        assert len(rid) == 32  # 32-char hex
        assert int(rid, 16) == span.get_span_context().trace_id


def test_correlation_id_helper_returns_none_outside_span():
    """No active span → None, caller's fallback (header / UUID) runs."""
    from server.observability.correlation_id import _try_trace_id
    assert _try_trace_id() is None


# ---------------------------------------------------------------------------
# make_subprocess_env — TRACEPARENT injection
# ---------------------------------------------------------------------------


def test_subprocess_env_injects_traceparent_inside_span(in_memory_tracing):
    """An agent subprocess spawned inside a span gets the parent's
    TRACEPARENT so its own OTel SDK can seed root context as a
    child of the web-server's span."""
    init_tracing()
    from server.utils.subprocess_env import make_subprocess_env

    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("parent"):
        env = make_subprocess_env()
        assert "TRACEPARENT" in env
        assert re.match(
            r"^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$", env["TRACEPARENT"]
        )


def test_subprocess_env_omits_traceparent_outside_span():
    """Outside any span, TRACEPARENT isn't set. The subprocess starts
    a fresh root trace rather than inheriting a stale id."""
    # Strip any TRACEPARENT bleeding in from the test runner's env.
    import os

    from server.utils.subprocess_env import make_subprocess_env
    os.environ.pop("TRACEPARENT", None)
    env = make_subprocess_env()
    assert "TRACEPARENT" not in env


# ---------------------------------------------------------------------------
# event_bus envelope — trace field
# ---------------------------------------------------------------------------


def test_event_bus_envelope_carries_traceparent_inside_span(in_memory_tracing):
    """Cross-replica delivery needs the publisher's traceparent so
    the receiving replica can link its delivery span back to the
    parent trace."""
    init_tracing()
    from server.websockets import event_bus
    from server.websockets.event_bus import BroadcastScope

    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("publisher-request"):
        raw = event_bus._serialize_envelope(
            BroadcastScope(), "task:log", {"line": "hello"}
        )
    obj = json.loads(raw)
    assert "trace" in obj
    assert "traceparent" in obj["trace"]
    assert re.match(
        r"^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$",
        obj["trace"]["traceparent"],
    )


def test_event_bus_envelope_omits_trace_field_outside_span():
    """Outside a span, the envelope omits the trace field. Older
    receivers (PR #171/#172) don't care; v1.1 receivers tolerate
    absence (next test)."""
    from server.websockets import event_bus
    from server.websockets.event_bus import BroadcastScope
    raw = event_bus._serialize_envelope(
        BroadcastScope(), "task:log", {"line": "hello"}
    )
    obj = json.loads(raw)
    assert "trace" not in obj


def test_event_bus_extract_traceparent_tolerates_old_envelopes():
    """Receivers running PR-2 of #42 must NOT crash on envelopes
    written by older replicas (no `trace` field). The helper just
    returns None and dispatch falls back to plain local delivery."""
    from server.websockets.event_bus import _extract_traceparent_from_raw

    old_envelope = json.dumps({
        "v": 1, "source": "x",
        "scope": {"kind": "broadcast"},
        "type": "t", "payload": {},
        # no trace field at all
    })
    assert _extract_traceparent_from_raw(old_envelope) is None


def test_event_bus_extract_traceparent_from_new_envelope():
    """When the trace field is present, extraction returns it."""
    from server.websockets.event_bus import _extract_traceparent_from_raw

    new_envelope = json.dumps({
        "v": 1, "source": "x",
        "scope": {"kind": "broadcast"},
        "type": "t", "payload": {},
        "trace": {
            "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
            "tracestate": "",
        },
    })
    tp = _extract_traceparent_from_raw(new_envelope)
    assert tp == "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
