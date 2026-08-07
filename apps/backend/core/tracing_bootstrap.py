"""Job/agent-subprocess side of OTel propagation (Epic #35 #42 PR-2, Factory#607).

The control-plane side of the chain lives in
``apps/web-server/server/observability/tracing.py`` (FastAPI + httpx
instrumentation, OTLP exporter) and in the vendored
``core/job_dispatch.trace_env`` (the ``TRACEPARENT`` + OTLP env a dispatched
Kubernetes Job receives). This module is what the *other side* — the agent
subprocess, and the build Job's ``run.py`` — calls at startup so its work shows
up inside the run's trace instead of ending it.

## Why this file changed (Factory#607)

It used to attach the inherited context and stop there, on purpose:

    We deliberately DO NOT add an OTLP exporter here [...] Spans build
    in-memory + drop, but critically the trace_id is preserved

That preserved the trace *id* for log correlation and threw away the trace.
The consequence was not subtle: the PARR work does not happen in the control
plane, it happens in a per-task Kubernetes Job, and across the collector's
whole retention window no job-side service name had ever appeared. A trace
covered the dispatch and stopped exactly where the time goes.

So this module now exports, under three constraints that the old comment was
right to worry about:

- **One span per process, not one per step.** ``cfactory-api`` alone produced
  4.9M spans in 30 days; a span per build step across every PARR run is how a
  k3d PV quietly fills. The Job contributes a single span covering its whole
  life, which is what answers "where did the run go". Anything the agent starts
  underneath it is a child of that span and costs whatever the agent chooses.
- **Only when there is a trace to join.** No ``TRACEPARENT`` means no parent,
  and an unparented job span is volume with no question attached — so this is a
  no-op for local CLI runs and dev sessions, exactly as before.
- **A short-lived process cannot be trusted to batch-flush on its own
  schedule.** The span is ended and flushed explicitly at exit with a bounded
  timeout (``_FLUSH_TIMEOUT_MS``), and the provider's own atexit shutdown —
  whose default budget is 30s — is disabled so a dead collector cannot hold a
  finished Job open. Bounded means bounded: a Job's exit is delayed by at most
  ``_FLUSH_TIMEOUT_MS`` when ``observe`` is down, and the spans drop.

## Failure-safe

Unchanged contract: any exception (OTel not installed, malformed traceparent,
exporter init failure) is caught and logged at WARNING. The agent's work always
proceeds. Tracing is never allowed to be the reason a build fails.
"""

from __future__ import annotations

import atexit
import logging
import os

logger = logging.getLogger(__name__)

# Module-level state — single attach token. Subsequent init calls
# are idempotent.
_initialized: bool = False
_attach_token = None
_job_span = None
_provider = None

# The budget an exiting Job may spend trying to deliver its spans. BOTH numbers
# are load-bearing and neither alone is enough — measured in-pod against a
# black-holed collector (192.0.2.1, which drops rather than refusing):
#
#   exporter default (10s deadline) + force_flush(3000) + shutdown() -> 20.54s
#   exporter timeout=2.0            + force_flush(3000), no shutdown ->  4.52s
#   ... the same code against the REAL collector                     ->  0.51s
#
# force_flush's timeout does not cancel an export already in flight, and
# TracerProvider.shutdown() then flushes AGAIN on the SDK's own 30s budget. So
# the exporter needs its own short deadline (same reasoning as Factory#608's
# startup probe), and shutdown() is deliberately not called: the worker is a
# daemon thread, so the process exits and the spans drop. A build must not be
# held open by a collector that is down.
_EXPORT_TIMEOUT_SECONDS = 2.0
_FLUSH_TIMEOUT_MS = 3000


def init_agent_tracing() -> None:
    """Join the dispatcher's trace and open the process's one span.

    Call this once near the top of every entry point that might be spawned by
    the web-server or dispatched as a Job (``cli.main``, agents/coder.py,
    runners/github/*). Idempotent.

    No-op when ``TRACEPARENT`` is not set — standalone CLI runs and dev
    sessions keep working untouched.
    """
    global _initialized, _attach_token, _job_span, _provider  # noqa: PLW0603
    if _initialized:
        return

    traceparent = os.environ.get("TRACEPARENT", "").strip()
    if not traceparent:
        logger.debug(
            "agent OTel bootstrap: TRACEPARENT not set; spans will be "
            "root-level if any are created"
        )
        _initialized = True
        return

    try:
        from opentelemetry import trace  # noqa: PLC0415
        from opentelemetry.context import attach  # noqa: PLC0415
        from opentelemetry.sdk.resources import Resource  # noqa: PLC0415
        from opentelemetry.sdk.trace import TracerProvider  # noqa: PLC0415
        from opentelemetry.trace.propagation.tracecontext import (  # noqa: PLC0415
            TraceContextTextMapPropagator,
        )
    except ImportError:
        logger.debug(
            "agent OTel bootstrap: opentelemetry packages not installed; "
            "skipping (agent will run untraced)"
        )
        _initialized = True
        return

    try:
        service_name = (
            os.environ.get("OTEL_SERVICE_NAME_AGENT")
            or os.environ.get("OTEL_SERVICE_NAME")
            or "aifactory-agent"
        )
        # shutdown_on_exit=False: the SDK's own atexit hook flushes with a 30s
        # default budget. _flush_at_exit below does it with a bounded one.
        provider = TracerProvider(
            resource=Resource.create({"service.name": service_name}),
            shutdown_on_exit=False,
        )
        # Only set the provider if the proxy is still in place —
        # respects any pre-installed provider (e.g. test fixtures).
        current = trace.get_tracer_provider()
        if isinstance(current, TracerProvider):
            provider = current
        else:
            trace.set_tracer_provider(provider)
        _provider = provider
        _install_exporter(provider)

        # Extract the parent context from the env var, then open THIS process's
        # span inside it and make that span current — so anything the agent
        # starts later is a child of it rather than a sibling of the dispatcher.
        parent_ctx = TraceContextTextMapPropagator().extract(
            {"traceparent": traceparent}
        )
        _job_span = trace.get_tracer(__name__).start_span(
            _span_name(), context=parent_ctx, attributes=_span_attributes()
        )
        _attach_token = attach(trace.set_span_in_context(_job_span, parent_ctx))
        atexit.register(_flush_at_exit)

        logger.info(
            "agent OTel bootstrap: inherited TRACEPARENT=%s service=%s span=%s",
            traceparent,
            service_name,
            _span_name(),
        )
        _initialized = True
    except Exception:  # noqa: BLE001 — tracing must never break the agent's work
        logger.warning(
            "agent OTel bootstrap failed; agent will run untraced",
            exc_info=True,
        )
        _initialized = True


def _span_name() -> str:
    """``<service> job`` — the dispatcher names both halves via env."""
    return f"{os.environ.get('FACTORY_SERVICE', 'aifactory')} job"


def _span_attributes() -> dict[str, str]:
    """The scalars that let an operator go from a span back to the run.

    ``factory.correlation_key`` is deliberately kept: it is the key every LOG
    line in this fleet already carries, so it is what joins a trace to the logs
    around it. Trace context replaced it as a *parenting* mechanism, not as an
    identifier.
    """
    attrs = {"factory.service": os.environ.get("FACTORY_SERVICE", "aifactory")}
    for env_name, attr in (
        ("JOB_ID", "factory.job_id"),
        ("CORRELATION_KEY", "factory.correlation_key"),
    ):
        value = os.environ.get(env_name, "").strip()
        if value:
            attrs[attr] = value
    return attrs


def _install_exporter(provider) -> None:
    """Add the OTLP exporter, or say plainly that spans will not land.

    No endpoint means no exporter: the Job still joins the trace (so its logs
    carry the run's trace_id) and its spans drop, which is the old behaviour and
    the right one off-cluster.
    """
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if not endpoint:
        logger.info(
            "agent OTel bootstrap: no OTEL_EXPORTER_OTLP_ENDPOINT — this "
            "process joins the trace for log correlation but its spans will "
            "NOT be exported (Factory#607)"
        )
        return
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # noqa: PLC0415
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.trace.export import BatchSpanProcessor  # noqa: PLC0415

        # BatchSpanProcessor, not SimpleSpanProcessor: batching is what makes
        # the export asynchronous, so a slow or dead collector drops spans off
        # the end of a bounded queue instead of blocking the build's hot path.
        # The "short-lived process can't be trusted to batch-flush" objection is
        # answered by _flush_at_exit, not by exporting synchronously.
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(timeout=_EXPORT_TIMEOUT_SECONDS))
        )
        logger.info(
            "agent OTel bootstrap: OTLP exporter installed endpoint=%s", endpoint
        )
    except Exception:  # noqa: BLE001 — tracing must never break the agent's work
        logger.warning(
            "agent OTel bootstrap: could not install the OTLP exporter; this "
            "process joins the trace but its spans will not be exported",
            exc_info=True,
        )


def _flush_at_exit() -> None:
    """End the process's span and push it, within a bounded budget."""
    try:
        if _job_span is not None:
            _job_span.end()
        if _provider is not None:
            # No shutdown() after this — see _EXPORT_TIMEOUT_SECONDS. It would
            # flush a second time on the SDK's own 30s budget, and the exiting
            # process does not need the worker thread stopped politely.
            _provider.force_flush(timeout_millis=_FLUSH_TIMEOUT_MS)
    except Exception:  # noqa: BLE001 — tracing must never break the agent's work
        logger.debug("agent OTel bootstrap: flush at exit failed", exc_info=True)


def get_inherited_traceparent() -> str | None:
    """Return the W3C ``traceparent`` the process inherited from its
    dispatcher, or None when standalone.

    Useful for log enrichment when full OTel isn't available — agents
    can still log ``traceparent={tp}`` to let operators stitch logs
    together by trace_id.
    """
    tp = os.environ.get("TRACEPARENT", "").strip()
    return tp or None
