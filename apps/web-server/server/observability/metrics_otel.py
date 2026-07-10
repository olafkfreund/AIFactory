"""OpenTelemetry per-worker / per-provider METRICS (Epic #45 P1).

Companion to ``tracing.py``: where that module exports *spans*, this one
exports the per-worker token / cost / duration *metrics* that make a
heterogeneous parallel AIFactory build observable per provider and per model.

## Why the metrics live in the web-server (not the agent)

The agent subprocess deliberately has NO OTel exporter — ``core/tracing_bootstrap.py``
builds spans in memory and drops them. The OTLP exporter lives here, in the
web-server, and is wired only when ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set (see
``tracing.py``). So the per-worker breakdown that the agent persists into
``token_usage.json`` (the ``workers`` map, read back by ``services/completion.py``)
is re-emitted as OTel metrics from *here*, at task end, where it can actually
export.

## Guard / no-op contract (ABSOLUTE)

This module is gated on the SAME enable condition as tracing:
``OTEL_EXPORTER_OTLP_ENDPOINT`` being non-empty. When it is empty (the Helm
default ``otel.enabled=false``):

  - ``record_worker_metrics()`` is a complete no-op: no MeterProvider is built,
    no instruments are created, no I/O happens, and nothing is imported beyond
    a cheap env read. Zero new runtime behavior, zero perf cost.

Like the rest of the observability layer it is failure-safe: any exception
(missing SDK, exporter down) is swallowed so a metric emit can never break the
completion path.

## Instruments + cardinality

Three instruments, ALL tagged with ONLY bounded-cardinality attributes
``{provider, model, phase}`` — NEVER ``task_id`` / ``worker_id`` /
``correlation_key`` (those would explode the time-series cardinality):

  - ``gen_ai.input_tokens``  (Counter)   — input tokens consumed
  - ``gen_ai.output_tokens`` (Counter)   — output tokens produced
  - ``gen_ai.cost_usd``      (Counter)   — USD cost (0 for local providers)
  - ``worker.duration_ms``   (Histogram) — per-worker wall-clock duration

## See

Design doc: docs/plans/2026-06-13-per-worker-observability-design.md
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# Module-level lazy singletons. Built on first use *only* when OTel is enabled.
_meter_provider = None  # type: ignore[assignment]
_input_tokens_counter = None  # type: ignore[assignment]
_output_tokens_counter = None  # type: ignore[assignment]
_cost_counter = None  # type: ignore[assignment]
_duration_histogram = None  # type: ignore[assignment]
_budget_exceeded_counter = None  # type: ignore[assignment]
_init_attempted: bool = False


def _otel_enabled() -> bool:
    """The single enable gate — mirrors ``tracing.py``'s exporter condition.

    Empty/unset ``OTEL_EXPORTER_OTLP_ENDPOINT`` (the Helm ``otel.enabled=false``
    default) means metrics are OFF and ``record_worker_metrics`` is a no-op.
    """
    return bool(os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip())


def _ensure_instruments() -> bool:
    """Lazily build the MeterProvider + the four instruments. Idempotent.

    Returns ``True`` when instruments are ready to record, ``False`` when OTel
    is disabled or initialisation failed (caller then no-ops). Best-effort —
    never raises.
    """
    global _meter_provider, _input_tokens_counter, _output_tokens_counter
    global _cost_counter, _duration_histogram, _budget_exceeded_counter
    global _init_attempted

    if _input_tokens_counter is not None:
        return True  # already built
    if not _otel_enabled():
        return False  # OFF — complete no-op
    if _init_attempted:
        # We tried once and didn't end up with instruments; don't retry-thrash.
        return _input_tokens_counter is not None
    _init_attempted = True

    try:
        from opentelemetry import metrics
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource

        service_name = os.environ.get("OTEL_SERVICE_NAME", "aifactory-web")

        # Respect a MeterProvider explicitly injected for tests, or one a test
        # fixture / prior init already set on the global — don't clobber an
        # InMemoryMetricReader-backed provider.
        if isinstance(_meter_provider, MeterProvider):
            pass  # already injected (test seam) — use it as-is
        elif isinstance(metrics.get_meter_provider(), MeterProvider):
            _meter_provider = metrics.get_meter_provider()
        else:
            protocol = (
                os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc").strip().lower()
            )
            if protocol == "http/protobuf":
                from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
                    OTLPMetricExporter,
                )
            else:
                from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
                    OTLPMetricExporter,
                )
            reader = PeriodicExportingMetricReader(OTLPMetricExporter())
            _meter_provider = MeterProvider(
                resource=Resource.create({"service.name": service_name}),
                metric_readers=[reader],
            )
            metrics.set_meter_provider(_meter_provider)
            logger.info(
                "OTel metrics enabled — exporting per-worker token/cost/duration "
                "(protocol=%s service=%s)",
                protocol,
                service_name,
            )

        meter = _meter_provider.get_meter("aifactory.worker_metrics")
        _input_tokens_counter = meter.create_counter(
            "gen_ai.input_tokens",
            unit="{token}",
            description="LLM input tokens consumed, by provider/model/phase.",
        )
        _output_tokens_counter = meter.create_counter(
            "gen_ai.output_tokens",
            unit="{token}",
            description="LLM output tokens produced, by provider/model/phase.",
        )
        _cost_counter = meter.create_counter(
            "gen_ai.cost_usd",
            unit="USD",
            description="LLM USD cost (0 for local providers), by provider/model/phase.",
        )
        _duration_histogram = meter.create_histogram(
            "worker.duration_ms",
            unit="ms",
            description="Per-worker wall-clock duration, by provider/model/phase.",
        )
        # Soft budget alert (#45 P2): a plain counter incremented once whenever a
        # task's rolled-up spend exceeds its contract budget. Bounded cardinality
        # by design — NO task_id / worker_id / correlation_key label (would
        # explode the series); an optional {provider:"aggregate"} marks it as a
        # whole-task aggregate rather than a per-provider series. OBSERVE-ONLY.
        _budget_exceeded_counter = meter.create_counter(
            "budget.exceeded",
            unit="{event}",
            description="Count of tasks whose rolled-up spend exceeded the contract budget.",
        )
        return True
    except Exception:
        logger.warning(
            "OTel metrics init failed; per-worker metrics won't export "
            "(completion path is unaffected)",
            exc_info=True,
        )
        return False


def _attrs(record: dict) -> dict:
    """Bounded-cardinality metric attributes for one worker record.

    ONLY ``{provider, model, phase}`` — never task/worker/correlation ids.
    Missing values bucket under ``"unknown"`` so a series is never keyed on
    ``None`` (which OTel rejects / drops).
    """
    return {
        "provider": record.get("provider") or "unknown",
        "model": record.get("model") or "unknown",
        "phase": record.get("phase") or "unknown",
    }


def record_worker_metrics(record: dict) -> None:
    """Emit the per-worker token/cost/duration metrics for ONE worker record.

    ``record`` is a single entry from the ``usage.workers[]`` list built by
    ``services/completion.read_usage`` (keys: provider/model/phase/
    input_tokens/output_tokens/cost_usd/duration_ms).

    Complete NO-OP when OTel is disabled (``OTEL_EXPORTER_OTLP_ENDPOINT``
    unset). Never raises — best-effort, like the rest of the observability layer.
    """
    if not isinstance(record, dict):
        return
    if not _ensure_instruments():
        return  # OTel off or init failed → no-op
    try:
        attrs = _attrs(record)
        in_tok = int(record.get("input_tokens", 0) or 0)
        out_tok = int(record.get("output_tokens", 0) or 0)
        cost = float(record.get("cost_usd", 0.0) or 0.0)
        dur = float(record.get("duration_ms", 0) or 0)

        if in_tok:
            _input_tokens_counter.add(in_tok, attrs)
        if out_tok:
            _output_tokens_counter.add(out_tok, attrs)
        # Cost counter is always added (including 0 for local providers) so the
        # per-provider series exists even when a provider is free.
        _cost_counter.add(cost, attrs)
        if dur:
            _duration_histogram.record(dur, attrs)
    except Exception:
        logger.debug("record_worker_metrics failed (best-effort)", exc_info=True)


def record_worker_metrics_batch(records: list[dict] | None) -> None:
    """Emit metrics for a list of worker records (the ``usage.workers[]`` list).

    Convenience wrapper over :func:`record_worker_metrics`. Complete no-op when
    OTel is disabled or ``records`` is falsy. Never raises.
    """
    if not records:
        return
    if not _otel_enabled():
        return  # short-circuit before any per-record work — guaranteed no-op
    for rec in records:
        record_worker_metrics(rec)


def record_budget_exceeded() -> None:
    """Increment the ``budget.exceeded`` counter by 1 (#45 P2 soft budget alert).

    Called from the completion path when a task's rolled-up spend exceeds its
    contract budget. Bounded cardinality: a single optional ``{provider:
    "aggregate"}`` attribute, never a task/worker/correlation id. Complete NO-OP
    when OTel is disabled (``OTEL_EXPORTER_OTLP_ENDPOINT`` unset). Never raises —
    best-effort, like the rest of this module. OBSERVE-ONLY: this only records a
    metric; it does not (and must not) abort, pause, or kill the build.
    """
    if not _ensure_instruments():
        return  # OTel off or init failed → no-op
    try:
        _budget_exceeded_counter.add(1, {"provider": "aggregate"})
    except Exception:
        logger.debug("record_budget_exceeded failed (best-effort)", exc_info=True)


def _reset_for_tests() -> None:
    """Drop the lazy singletons so a test can re-init under a fresh fixture.

    Test-only helper; the production code never calls this.
    """
    global _meter_provider, _input_tokens_counter, _output_tokens_counter
    global _cost_counter, _duration_histogram, _budget_exceeded_counter
    global _init_attempted
    _meter_provider = None
    _input_tokens_counter = None
    _output_tokens_counter = None
    _cost_counter = None
    _duration_histogram = None
    _budget_exceeded_counter = None
    _init_attempted = False
