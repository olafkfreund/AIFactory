"""Agent-subprocess side of OTel propagation (Epic #35 #42 PR-2).

The web-server side of the chain lives in
``apps/web-server/server/observability/tracing.py`` — that module
opens a span around each phase transition and injects the W3C
``TRACEPARENT`` env var into the subprocess environment via
``make_subprocess_env``. This module is what the agent subprocess
calls at startup to *receive* that context so its own spans become
children of the web-server's span.

## When does the agent need this?

Only when both are true:
  1. The web-server is exporting traces
     (``OTEL_EXPORTER_OTLP_ENDPOINT`` set in the pod env)
  2. The agent subprocess inherited a ``TRACEPARENT`` env var
     (web-server was inside an active span when it spawned us)

When the agent runs standalone (CLI, local dev, no collector), this
module quietly no-ops. The agent's own work executes unchanged.

## What it does

1. Reads ``TRACEPARENT`` from os.environ.
2. If absent: does nothing.
3. If present: installs a minimal OTel TracerProvider so the agent
   has somewhere to emit spans, then extracts the parent context
   from the env var and activates it via
   ``opentelemetry.context.attach``. Subsequent ``start_span()``
   calls in the agent inherit the trace_id and link to the
   web-server's span.

## Failure-safe

Same contract as the web-server side: any exception (OTel not
installed, malformed traceparent, exporter init failure) is caught
and logged at WARNING. The agent's work always proceeds.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# Module-level state — single attach token. Subsequent init calls
# are idempotent.
_initialized: bool = False
_attach_token = None


def init_agent_tracing() -> None:
    """Bootstrap OTel context from the inherited ``TRACEPARENT``.

    Call this once near the top of every agent entry point that
    might be spawned by the web-server (e.g. agents/coder.py,
    agents/planner.py, runners/github/*). Idempotent.

    No-op when ``TRACEPARENT`` is not set in the environment —
    standalone CLI runs and dev sessions keep working untouched.
    """
    global _initialized, _attach_token
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
        from opentelemetry import trace
        from opentelemetry.context import attach
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.trace.propagation.tracecontext import (
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
        # Install a minimal TracerProvider. We deliberately DO NOT
        # add an OTLP exporter here — the agent's spans would
        # duplicate / fight with the web-server's exporter, and the
        # agent process is short-lived enough that batch-flushing on
        # exit is unreliable. Spans build in-memory + drop, but
        # critically the trace_id is preserved so log lines that
        # interpolate it (via structlog or similar) carry the parent
        # trace's identifier.
        service_name = os.environ.get("OTEL_SERVICE_NAME_AGENT", "aifactory-agent")
        provider = TracerProvider(
            resource=Resource.create({"service.name": service_name})
        )
        # Only set the provider if the proxy is still in place —
        # respects any pre-installed provider (e.g. test fixtures).
        current = trace.get_tracer_provider()
        if not isinstance(current, TracerProvider):
            trace.set_tracer_provider(provider)

        # Extract the parent context from the env var and attach it
        # as the current context. Subsequent get_current_span /
        # start_span calls now inherit the trace_id.
        propagator = TraceContextTextMapPropagator()
        carrier = {"traceparent": traceparent}
        parent_ctx = propagator.extract(carrier)
        _attach_token = attach(parent_ctx)

        logger.info(
            "agent OTel bootstrap: inherited TRACEPARENT=%s service=%s",
            traceparent,
            service_name,
        )
        _initialized = True
    except Exception:
        logger.warning(
            "agent OTel bootstrap failed; agent will run untraced",
            exc_info=True,
        )
        _initialized = True


def get_inherited_traceparent() -> str | None:
    """Return the W3C ``traceparent`` value the agent inherited from
    the web-server, or None when standalone.

    Useful for log enrichment when full OTel isn't available — agents
    can still log ``traceparent={tp}`` to let operators stitch logs
    together by trace_id.
    """
    tp = os.environ.get("TRACEPARENT", "").strip()
    return tp or None
