"""Job-side OTel bootstrap: the far half of ``job_dispatch.trace_env``.

CANONICAL — this file is the single source of truth (Factory#638). It is
hand-vendored **byte-exact** into the services that dispatch Kubernetes Jobs:

    AIFactory   apps/backend/core/job_tracing.py
    TFactory    apps/backend/tools/runners/job_tracing.py

``scripts/check_verification_core_drift.py`` compares those copies against this
one on every PR in every consumer. Change this file first, re-vendor, bump each
consumer's ``HUB_PIN_SHA``; never edit a vendored copy in place. A second
hand-written copy "based on" this one is the exact defect the drift gate exists
to prevent — the pod-label bug in ``job_dispatch`` arrived three times that way.

## The two halves

``job_dispatch.trace_env(service)`` is the DISPATCHER's half: it puts
``TRACEPARENT`` plus the OTLP endpoint/protocol/headers on the Job manifest, and
names the Job's spans ``<service>-job``. This module is the JOB's half: it joins
that trace and emits. Neither half is worth anything alone, and the failure
shapes are not symmetric:

* Emitter without env — the Job runs untraced. An honest gap. Nothing claims
  otherwise.
* Env without emitter — the Job carries correct-looking trace configuration and
  produces nothing. That reads as coverage on inspection, and the trace still
  stops at the seam. This is strictly worse, which is why Factory#638 refused
  the one-line ``env.extend(trace_env(SERVICE))`` on TFactory's verify Job until
  this file existed to land beside it.

## Why it exports at all (Factory#607)

AIFactory's predecessor to this module attached the inherited context and
stopped there, on purpose:

    We deliberately DO NOT add an OTLP exporter here [...] Spans build
    in-memory + drop, but critically the trace_id is preserved

That preserved the trace *id* for log correlation and threw away the trace. The
consequence was not subtle: the PARR work does not happen in the control plane,
it happens in a per-task Kubernetes Job, and across the collector's whole
retention window no job-side service name had ever appeared. A trace covered the
dispatch and stopped exactly where the time goes.

So this module exports, under three constraints the old comment was right to
worry about:

- **One span per process, not one per step.** ``cfactory-api`` alone produced
  4.9M spans in 30 days; a span per step across every PARR run is how a k3d PV
  quietly fills. The Job contributes a single span covering its whole life,
  which is what answers "where did the run go". Anything the process starts
  underneath it is a child of that span and costs whatever that caller chooses.
- **Only when there is a trace to join.** No ``TRACEPARENT`` means no parent,
  and an unparented job span is volume with no question attached — so this is a
  no-op for local CLI runs and dev sessions.
- **A short-lived process cannot be trusted to batch-flush on its own
  schedule.** The span is ended and flushed explicitly at exit on a bounded
  budget, and the provider's own atexit shutdown — whose default is 30s — is
  disabled, so a dead collector cannot hold a finished Job open.

## Failure-safe

Any exception (OTel not installed, malformed traceparent, exporter init
failure) is caught and logged at WARNING. The process's real work always
proceeds. Tracing is never allowed to be the reason a build or a verify fails.
"""

from __future__ import annotations

import atexit
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# Module-level state — single attach token. Subsequent init calls are idempotent.
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
# daemon thread, so the process exits and the spans drop. A Job must not be held
# open by a collector that is down.
_EXPORT_TIMEOUT_SECONDS = 2.0
_FLUSH_TIMEOUT_MS = 3000

# Fallback when the dispatcher named neither the service nor the span. Every
# real dispatch sets FACTORY_SERVICE (job_dispatch puts it on the manifest), so
# this is only ever seen by a hand-run process.
_DEFAULT_SERVICE = "factory"


def init_agent_tracing() -> None:
    """Join the dispatcher's trace and open this process's one span.

    Call this once at the top of every entry point that a control plane may
    dispatch as a Job or spawn as a subprocess. Idempotent.

    No-op when ``TRACEPARENT`` is not set — standalone CLI runs and dev sessions
    keep working untouched.
    """
    global _initialized, _attach_token, _job_span, _provider  # noqa: PLW0603
    if _initialized:
        return

    traceparent = os.environ.get("TRACEPARENT", "").strip()
    if not traceparent:
        logger.debug(
            "job OTel bootstrap: TRACEPARENT not set; spans will be root-level if any are created"
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
            "job OTel bootstrap: opentelemetry packages not installed; skipping (this "
            "process will run untraced)"
        )
        _initialized = True
        return

    try:
        service_name = _service_name()
        # shutdown_on_exit=False: the SDK's own atexit hook flushes with a 30s
        # default budget. _flush_at_exit below does it with a bounded one.
        provider = TracerProvider(
            resource=Resource.create({"service.name": service_name}), shutdown_on_exit=False
        )
        # Only set the provider if the proxy is still in place — respects any
        # pre-installed provider (e.g. test fixtures).
        current = trace.get_tracer_provider()
        if isinstance(current, TracerProvider):
            provider = current
        else:
            trace.set_tracer_provider(provider)
        _provider = provider
        _install_exporter(provider)

        # Extract the parent context from the env var, then open THIS process's
        # span inside it and make that span current — so anything started later
        # is a child of it rather than a sibling of the dispatcher.
        parent_ctx = TraceContextTextMapPropagator().extract({"traceparent": traceparent})
        _job_span = trace.get_tracer(__name__).start_span(
            _span_name(), context=parent_ctx, attributes=_span_attributes()
        )
        _attach_token = attach(trace.set_span_in_context(_job_span, parent_ctx))
        atexit.register(_flush_at_exit)

        logger.info(
            "job OTel bootstrap: inherited TRACEPARENT=%s service=%s span=%s",
            traceparent,
            service_name,
            _span_name(),
        )
        _initialized = True
    except Exception:  # noqa: BLE001 — tracing must never break the process's work
        logger.warning("job OTel bootstrap failed; this process will run untraced", exc_info=True)
        _initialized = True


def _service_name() -> str:
    """The collector-visible service name for this process's spans.

    ``OTEL_SERVICE_NAME`` is what ``job_dispatch.trace_env`` sets, to
    ``<service>-job`` — the shape ``trace-silence-check``'s join assertion
    matches on (``service_name LIKE '%-job'``), and the reason job-side names
    are deliberately absent from its ``EXPECTED_SERVICES`` list: Jobs are
    episodic, so "no spans this hour" is idleness, not breakage.
    ``OTEL_SERVICE_NAME_AGENT`` overrides it for the in-process subprocess path,
    where the parent's OTEL_SERVICE_NAME is inherited verbatim and would
    otherwise make the child indistinguishable from it.
    """
    return (
        os.environ.get("OTEL_SERVICE_NAME_AGENT")
        or os.environ.get("OTEL_SERVICE_NAME")
        or f"{_factory_service()}-job"
    )


def _factory_service() -> str:
    """``FACTORY_SERVICE``, which every dispatched Job carries."""
    return os.environ.get("FACTORY_SERVICE", "").strip() or _DEFAULT_SERVICE


def _span_name() -> str:
    """``<service> job`` — the dispatcher names both halves via env."""
    return f"{_factory_service()} job"


def _span_attributes() -> dict[str, str]:
    """The scalars that let an operator go from a span back to the run.

    ``factory.correlation_key`` is deliberately kept: it is the key every LOG
    line in this fleet already carries, so it is what joins a trace to the logs
    around it. Trace context replaced it as a *parenting* mechanism, not as an
    identifier.
    """
    attrs = {"factory.service": _factory_service()}
    for env_name, attr in (
        ("JOB_ID", "factory.job_id"),
        ("CORRELATION_KEY", "factory.correlation_key"),
    ):
        value = os.environ.get(env_name, "").strip()
        if value:
            attrs[attr] = value
    return attrs


def _install_exporter(provider: Any) -> None:
    """Add the OTLP exporter, or say plainly that spans will not land.

    No endpoint means no exporter: the process still joins the trace (so its
    logs carry the run's trace_id) and its spans drop, which is the right
    behaviour off-cluster.
    """
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if not endpoint:
        logger.info(
            "job OTel bootstrap: no OTEL_EXPORTER_OTLP_ENDPOINT — this process joins "
            "the trace for log correlation but its spans will NOT be exported "
            "(Factory#607)"
        )
        return
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # noqa: PLC0415
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.trace.export import BatchSpanProcessor  # noqa: PLC0415

        # BatchSpanProcessor, not SimpleSpanProcessor: batching is what makes
        # the export asynchronous, so a slow or dead collector drops spans off
        # the end of a bounded queue instead of blocking the work's hot path.
        # The "short-lived process can't be trusted to batch-flush" objection is
        # answered by _flush_at_exit, not by exporting synchronously.
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(timeout=_EXPORT_TIMEOUT_SECONDS))
        )
        logger.info("job OTel bootstrap: OTLP exporter installed endpoint=%s", endpoint)
    except Exception:  # noqa: BLE001 — tracing must never break the process's work
        logger.warning(
            "job OTel bootstrap: could not install the OTLP exporter; this process "
            "joins the trace but its spans will not be exported",
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
    except Exception:  # noqa: BLE001 — tracing must never break the process's work
        logger.debug("job OTel bootstrap: flush at exit failed", exc_info=True)


def get_inherited_traceparent() -> str | None:
    """Return the W3C ``traceparent`` this process inherited from its dispatcher,
    or None when standalone.

    Useful for log enrichment when full OTel isn't available — a process can
    still log ``traceparent={tp}`` to let operators stitch logs together by
    trace_id.
    """
    tp = os.environ.get("TRACEPARENT", "").strip()
    return tp or None
