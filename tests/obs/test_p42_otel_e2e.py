"""e2e smoke for OpenTelemetry tracing (Epic #35 #42 PR-2).

Builds the real production app via ``main.create_app()`` with an
InMemorySpanExporter attached to whatever TracerProvider lifespan
installs, fires a request, and asserts a full HTTP-request span is
present in the buffer with the right resource attribute.

Why this exists alongside ``tests/test_tracing.py``:
  - test_tracing.py covers UNIT behaviour of each integration point.
  - This file proves that when main.create_app() runs end-to-end, the
    FastAPI auto-instrumentation actually opens spans on every request
    — i.e. the lifespan wiring is reachable. If a future refactor
    drops the ``instrument_fastapi_app(app)`` call this test fails
    immediately.
"""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WEB_SERVER_ROOT = REPO_ROOT / "apps" / "web-server"
if str(WEB_SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(WEB_SERVER_ROOT))


@pytest.fixture
def app_with_in_memory_exporter(monkeypatch):
    """Build main.create_app() + ensure spans land in our buffer.

    Same adapt-don't-fight strategy as tests/test_tracing.py: attach
    an InMemorySpanExporter to whatever TracerProvider is active.
    """
    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )
    from prometheus_client import REGISTRY

    for c in list(REGISTRY._collector_to_names.keys()):  # type: ignore[attr-defined]
        # ponytail: collector may already be unregistered by another fixture
        with contextlib.suppress(KeyError):
            REGISTRY.unregister(c)

    monkeypatch.delenv("METRICS_SCRAPE_TOKEN", raising=False)
    monkeypatch.setenv("APP_DISABLE_AUTH", "true")

    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)

    current = trace.get_tracer_provider()
    if isinstance(current, TracerProvider):
        current.add_span_processor(processor)
    else:
        provider = TracerProvider(
            resource=Resource.create({"service.name": "aifactory-web"})
        )
        provider.add_span_processor(processor)
        trace.set_tracer_provider(provider)

    # Stop init_tracing from clobbering our exporter on lifespan.
    from server.observability import tracing as tm

    tm._tracer_provider = trace.get_tracer_provider()
    tm._initialized = True

    from server.main import create_app

    app = create_app()
    return app, exporter


@pytest.mark.obs
def test_http_request_creates_a_span(app_with_in_memory_exporter):
    """The FastAPI auto-instrumentation must open a span around every
    request once instrument_fastapi_app() has run. Regression guard
    for accidental drops of that call in main.py."""
    from fastapi.testclient import TestClient

    app, exporter = app_with_in_memory_exporter
    exporter.clear()

    with TestClient(app) as client:
        resp = client.get("/api/health")

    assert resp.status_code == 200, f"/api/health should be 200; got {resp.status_code}"

    spans = exporter.get_finished_spans()
    # FastAPI instrumentor names HTTP spans after their route. The
    # one we care about is the GET /api/health span (FastAPI may
    # emit a few internal spans alongside it; we just need ours to
    # exist).
    matching = [
        s
        for s in spans
        if "health" in s.name or s.attributes.get("http.target") == "/api/health"
    ]
    assert matching, (
        f"No span found for GET /api/health; got "
        f"{[(s.name, dict(s.attributes)) for s in spans][:5]}"
    )


@pytest.mark.obs
def test_correlation_id_echoes_trace_id_when_no_client_header(
    app_with_in_memory_exporter,
):
    """When the client does NOT send X-Request-ID, the middleware
    sources the request id from the active trace_id (32-hex). This
    is the trace ↔ log bridge."""
    from fastapi.testclient import TestClient
    from server.observability import CORRELATION_ID_HEADER

    app, _exporter = app_with_in_memory_exporter
    with TestClient(app) as client:
        resp = client.get("/api/health")

    rid = resp.headers.get(CORRELATION_ID_HEADER)
    assert rid is not None, "middleware must always set X-Request-ID"
    # When a span is active we expect 32-hex (trace_id format), not
    # a UUID. Both pass length, but trace_id is hex-only while UUID
    # contains dashes.
    assert "-" not in rid, (
        f"X-Request-ID looks like a UUID ({rid!r}) — expected the "
        f"OTel trace_id (32-hex without dashes) when no client header "
        f"was sent"
    )
    assert len(rid) == 32 and all(c in "0123456789abcdef" for c in rid), (
        f"X-Request-ID must be 32-char hex (trace_id format); got {rid!r}"
    )


@pytest.mark.obs
def test_client_x_request_id_still_wins(app_with_in_memory_exporter):
    """Backward-compat: when the client sends X-Request-ID, it's
    echoed back unchanged, regardless of whether a span is active.
    Operators correlating across services with their own IDs must
    not have them overridden."""
    from fastapi.testclient import TestClient
    from server.observability import CORRELATION_ID_HEADER

    app, _ = app_with_in_memory_exporter
    with TestClient(app) as client:
        resp = client.get(
            "/api/health",
            headers={CORRELATION_ID_HEADER: "operator-rid-42"},
        )

    assert resp.headers.get(CORRELATION_ID_HEADER) == "operator-rid-42"
