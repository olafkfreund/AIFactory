"""OpenTelemetry distributed tracing (Epic #35 #42 PR-1).

Adds end-to-end traces so operators can answer "where did this
request spend its time?" with one Tempo / Jaeger / Datadog query.

## Optional remote

When ``OTEL_EXPORTER_OTLP_ENDPOINT`` is empty (default), this module
installs a tracer provider with **no exporter** — spans still get
created by auto-instrumentation (cheap, in-memory) and are dropped
at export time. Zero observable cost when off.

When the endpoint env is set, an OTLPSpanExporter is installed with
a BatchSpanProcessor. Auto-instrumentation is enabled either way so
the surface stays uniform across modes.

## Per-phase manual spans

``task_phase_span(task_id, phase)`` wraps the agent-task lifecycle's
four phase transitions (planning / coding / review_pending /
completed). Becomes a child of the surrounding HTTP request span
when one exists; root span otherwise.

## Failure-safe

Exporter failures (network down, credential expired, collector OOM)
log a WARNING via the OTel SDK's own machinery and drop spans. The
calling code path is never crashed by tracing.

## The "enabled" line is checked, not claimed (Factory#465)

Constructing an ``OTLPSpanExporter`` touches no network, so logging
"OTel tracing enabled" straight after it says nothing about whether
a span will ever land. It said nothing for 35 days while every batch
came back 401 and the whole subsystem was dark.

So ``init_tracing()`` now spends one empty OTLP POST proving the
endpoint and the credential before it makes any claim: SUCCESS logs
the enabled line as before, FAILURE logs an ERROR that names the
endpoint, the protocol and the auth scheme. The exporter is
installed either way — a collector that is merely restarting must
still be able to recover without a pod bounce.

The same probe answers the second half of #465: a persistently
rejected exporter used to emit ~12 identical ERROR lines a minute,
199 of the last 300 log lines, which is how a genuine error gets
missed. The SDK exporter's own logger is rate-limited to one line
per interval plus a suppressed-count summary.

## See

Design doc: docs/plans/2026-05-28-otel-tracing-design.md
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Generator

logger = logging.getLogger(__name__)

# How long a rejected exporter stays quiet between log lines, seconds.
_EXPORT_ERROR_INTERVAL = float(os.environ.get("OTEL_EXPORT_ERROR_INTERVAL", "300"))

# Module-level state — set on first init_tracing() call. Subsequent
# calls no-op so tests can re-init cheaply.
_initialized: bool = False
_tracer_provider = None  # type: ignore[assignment]


def init_tracing() -> None:
    """Wire up the OTel tracer provider + auto-instrumentation.

    Idempotent. Called from ``main.py`` app lifespan. Reads:

    - ``OTEL_EXPORTER_OTLP_ENDPOINT`` — empty = no exporter
    - ``OTEL_SERVICE_NAME`` — defaults to ``aifactory-web``
    - ``OTEL_TRACES_SAMPLER`` / ``OTEL_TRACES_SAMPLER_ARG`` — read by
      the SDK itself; we don't need to thread them through
    - ``OTEL_EXPORTER_OTLP_PROTOCOL`` / ``OTEL_EXPORTER_OTLP_HEADERS``
      — read by the exporter package

    Safe to call from test fixtures with a different exporter
    pre-installed; this function detects that and skips reinstall.
    """
    global _initialized, _tracer_provider
    if _initialized:
        return

    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        service_name = os.environ.get("OTEL_SERVICE_NAME", "aifactory-web")
        resource = Resource.create({"service.name": service_name})

        # If a test fixture has already installed a TracerProvider
        # (e.g. with an InMemorySpanExporter), respect it — don't
        # clobber. Detected via the SDK's get_tracer_provider() return
        # being a TracerProvider instance from our SDK (vs the default
        # ProxyTracerProvider).
        current_provider = trace.get_tracer_provider()
        if isinstance(current_provider, TracerProvider):
            _tracer_provider = current_provider
        else:
            _tracer_provider = TracerProvider(resource=resource)
            trace.set_tracer_provider(_tracer_provider)

        # Install the OTLP exporter only when an endpoint is set.
        # Without it, spans build in memory + drop with no I/O.
        endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
        if endpoint:
            try:
                # Pick the right exporter based on protocol. The OTLP
                # umbrella package ships both gRPC + HTTP variants.
                protocol = (
                    os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc")
                    .strip()
                    .lower()
                )
                if protocol == "http/protobuf":
                    from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                        OTLPSpanExporter,
                    )
                else:
                    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                        OTLPSpanExporter,
                    )
                exporter = OTLPSpanExporter()
                # Quieten the SDK exporter BEFORE the probe, so a broken
                # credential costs one line here and not one every 5s
                # forever after (Factory#465).
                rate_limit_exporter_log(exporter)
                _tracer_provider.add_span_processor(BatchSpanProcessor(exporter))
                _verify_export_auth(exporter, endpoint, protocol, service_name)
            except Exception:
                logger.warning(
                    "Failed to install OTLP exporter; tracing degrades to "
                    "build-in-memory + drop. Spans still construct cheaply.",
                    exc_info=True,
                )
        else:
            logger.info(
                "OTel tracing initialised without exporter "
                "(OTEL_EXPORTER_OTLP_ENDPOINT unset) — spans build in "
                "memory + drop at export. service=%s",
                service_name,
            )

        # Auto-instrumentation. Each .instrument() is idempotent in the
        # OTel contrib SDK (subsequent calls no-op), so safe to call
        # from re-entered init_tracing(). Wrapped in best-effort
        # try/except so a broken instrumentor doesn't crash startup.
        _instrument_libraries()

        _initialized = True
    except Exception:
        logger.warning(
            "init_tracing() failed; continuing without distributed tracing",
            exc_info=True,
        )


class _RateLimitFilter(logging.Filter):
    """Pass one record per ``interval``; count and summarise the rest.

    ponytail: one counter, not one per message key. The OTLP exporter
    logs a single message shape ("Failed to export ... code: N"), so
    keying by message would buy nothing and cost a dict.
    """

    def __init__(self, interval: float) -> None:
        super().__init__()
        self._interval = interval
        self._last = 0.0
        self._suppressed = 0

    def filter(self, record: logging.LogRecord) -> bool:
        now = time.monotonic()
        if self._last and now - self._last < self._interval:
            self._suppressed += 1
            return False
        if self._suppressed:
            record.msg = (
                f"{record.getMessage()} [+{self._suppressed} identical messages "
                f"suppressed in the previous {self._interval:.0f}s]"
            )
            record.args = ()
            self._suppressed = 0
        self._last = now
        return True


def rate_limit_exporter_log(exporter) -> None:
    """Attach the rate limiter to whichever SDK logger this exporter uses.

    Derived from the exporter's own module so it is exact for both the
    http and grpc variants — a filter on a parent logger would not
    apply, because filters do not run on propagated records.
    """
    try:
        logging.getLogger(type(exporter).__module__).addFilter(
            _RateLimitFilter(_EXPORT_ERROR_INTERVAL)
        )
    except Exception:  # noqa: BLE001 — noise control must never break startup
        logger.debug("could not rate-limit the OTLP exporter log", exc_info=True)


def _verify_export_auth(
    exporter, endpoint: str, protocol: str, service_name: str
) -> None:
    """Prove the endpoint and credential with one empty export.

    ``export([])`` posts an empty OTLP payload: it costs one request,
    carries no span data, and still round-trips the auth header, so it
    separates "configured" from "actually accepted". Measured against
    OpenObserve v0.90.3 on 2026-07-30: a Bearer header returns FAILURE
    (401 Not Supported), Basic root credentials return SUCCESS.

    Never raises. An exporter that cannot be probed is left installed
    and reported as unverified rather than torn down.
    """
    try:
        from opentelemetry.sdk.trace.export import SpanExportResult

        ok = exporter.export([]) is SpanExportResult.SUCCESS
    except Exception:  # noqa: BLE001 — an unprobeable exporter still exports
        logger.warning(
            "OTel tracing enabled but UNVERIFIED — the startup export probe "
            "could not run. endpoint=%s protocol=%s service=%s",
            endpoint,
            protocol,
            service_name,
            exc_info=True,
        )
        return

    if ok:
        logger.info(
            "OTel tracing enabled — endpoint=%s protocol=%s service=%s "
            "(startup export accepted)",
            endpoint,
            protocol,
            service_name,
        )
        return

    logger.error(
        "OTel tracing is configured but the collector REJECTED the startup "
        "export: NO SPANS WILL LAND. endpoint=%s protocol=%s service=%s "
        "auth=%s. Check OTEL_EXPORTER_OTLP_HEADERS against what the collector "
        "accepts (Factory#465).",
        endpoint,
        protocol,
        service_name,
        _auth_scheme(),
    )


def _auth_scheme() -> str:
    """The auth scheme name from OTEL_EXPORTER_OTLP_HEADERS, never its value.

    Naming the scheme is the whole diagnosis for #465 ("Bearer" where the
    collector only accepts "Basic") and it leaks nothing.
    """
    raw = os.environ.get("OTEL_EXPORTER_OTLP_HEADERS", "")
    for part in raw.split(","):
        name, _, value = part.partition("=")
        if name.strip().lower() == "authorization":
            return value.strip().split(" ")[0] or "<empty>"
    return "<no Authorization header>"


def _instrument_libraries() -> None:
    """Best-effort instrumentation of the 5 libraries the issue lists.

    Each instrumentor is wrapped individually so one failing import
    doesn't block the others. Per-library failures log + continue —
    the spec's failure-safe contract.
    """
    _instrument_one("FastAPI", _instrument_fastapi)
    _instrument_one("SQLAlchemy", _instrument_sqlalchemy)
    _instrument_one("asyncpg", _instrument_asyncpg)
    _instrument_one("httpx", _instrument_httpx)
    _instrument_one("redis", _instrument_redis)


def _instrument_one(name: str, fn) -> None:
    try:
        fn()
    except Exception:
        logger.warning(
            "OTel %s instrumentation failed; spans for it won't appear",
            name,
            exc_info=True,
        )


def _instrument_fastapi() -> None:
    # FastAPI auto-instrumentation needs the app object. We DON'T
    # call FastAPIInstrumentor.instrument_app() here — main.py does
    # it explicitly so the order is visible. This stub is kept so the
    # _instrument_one loop has uniform shape; the actual hook lands
    # in main.py via ``instrument_fastapi_app(app)`` below.
    pass


def _instrument_sqlalchemy() -> None:
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

    SQLAlchemyInstrumentor().instrument()


def _instrument_asyncpg() -> None:
    from opentelemetry.instrumentation.asyncpg import AsyncPGInstrumentor

    AsyncPGInstrumentor().instrument()


def _instrument_httpx() -> None:
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

    HTTPXClientInstrumentor().instrument()


def _instrument_redis() -> None:
    from opentelemetry.instrumentation.redis import RedisInstrumentor

    RedisInstrumentor().instrument()


def instrument_fastapi_app(app) -> None:
    """Wire OTel into a specific FastAPI app instance. Called from
    main.py after ``init_tracing()`` so the app's instance is in scope.

    Idempotent — the contrib SDK detects double-init.
    """
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app)
    except Exception:
        logger.warning(
            "OTel FastAPI instrumentation failed; HTTP request spans won't appear",
            exc_info=True,
        )


@contextmanager
def task_phase_span(
    task_id: str,
    phase: str,
    *,
    provider: str | None = None,
    model: str | None = None,
) -> Generator[object, None, None]:
    """Open a span named ``task:phase:{phase}`` with attributes
    ``task.id`` + ``task.phase``.

    Becomes a child of the surrounding HTTP request span when one
    exists (because FastAPI auto-instrumentation has already opened
    one); root span otherwise. Use as::

        with task_phase_span(task_id, "coding") as span:
            ...

    Optional ``provider`` / ``model`` (#45 P1) attach bounded-cardinality
    ``gen_ai.provider`` / ``gen_ai.request.model`` span attributes when a
    caller knows them — additive, omitted when ``None`` so existing call
    sites behave exactly as before.

    When OTel SDK isn't available (or init_tracing has not run yet),
    yields a no-op object that callers can safely interact with via
    duck-typed ``set_attribute`` calls — keeps the integration sites
    free of import-time guards.
    """
    try:
        from opentelemetry import trace

        tracer = trace.get_tracer("aifactory.agent_task")
        with tracer.start_as_current_span(f"task:phase:{phase}") as span:
            span.set_attribute("task.id", task_id)
            span.set_attribute("task.phase", phase)
            if provider:
                span.set_attribute("gen_ai.provider", provider)
            if model:
                span.set_attribute("gen_ai.request.model", model)
            yield span
    except Exception:
        # If OTel imports fail or anything else goes wrong, yield a
        # no-op stand-in so the caller's `with` block runs cleanly.
        yield _NullSpan()


class _NullSpan:
    """Duck-typed no-op span. Used when OTel isn't installed or
    init failed. Callers can `.set_attribute(...)` etc. without
    raising."""

    def set_attribute(self, *a, **kw) -> None:
        pass

    def set_status(self, *a, **kw) -> None:
        pass

    def record_exception(self, *a, **kw) -> None:
        pass


def get_current_traceparent() -> str | None:
    """Return the W3C ``traceparent`` header string for the current
    span, or None when no span is active.

    Used by ``subprocess_env`` to inject ``TRACEPARENT`` and by
    ``event_bus`` to inject into the envelope's ``trace`` field so
    cross-process / cross-replica handlers can re-link spans.

    The W3C header format is:
        ``00-{trace_id_hex_32}-{span_id_hex_16}-{flags_hex_2}``
    """
    try:
        from opentelemetry import trace

        span = trace.get_current_span()
        if span is None:
            return None
        ctx = span.get_span_context()
        if not ctx.is_valid:
            return None
        # Format the W3C traceparent. trace_id and span_id are ints;
        # format with leading zeros to the expected hex widths.
        return f"00-{ctx.trace_id:032x}-{ctx.span_id:016x}-{ctx.trace_flags:02x}"
    except Exception:
        return None
