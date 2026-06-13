"""Tests for OTel per-worker / per-provider metrics (Epic #45 P1).

Covers ``server.observability.metrics_otel`` and the ``_new_traceparent``
active-span linkage in ``server.services.completion``.

Key assertions:
  - With OTel enabled (endpoint set) and an InMemoryMetricReader installed,
    a 2-worker batch (claude + ollama, ollama cost_usd=0) records the
    instruments with correct ``{provider, model, phase}`` attributes + values.
  - With OTel disabled (no endpoint), ``record_worker_metrics`` is a complete
    NO-OP: no error, no instruments built, no metrics produced.
  - ``_new_traceparent`` uses the active span context when one is set and
    falls back to the synthetic random traceparent otherwise.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_WS = Path(__file__).resolve().parents[1]
if str(_WS) not in sys.path:
    sys.path.insert(0, str(_WS))

_TRACEPARENT_RE = re.compile(r"^[0-9a-f]{2}-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$")


def _worker(provider, model, *, phase="coding", in_t, out_t, cost, dur):
    return {
        "worker_id": f"w-{provider}",
        "phase": phase,
        "subtask_id": f"w-{provider}",
        "provider": provider,
        "model": model,
        "input_tokens": in_t,
        "output_tokens": out_t,
        "total_tokens": in_t + out_t,
        "cost_usd": cost,
        "duration_ms": dur,
    }


def _collect(reader):
    """Flatten an InMemoryMetricReader into {metric_name: [(attrs, value)]}."""
    out: dict[str, list] = {}
    data = reader.get_metrics_data()
    if data is None:
        return out
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                pts = []
                for dp in metric.data.data_points:
                    # Counters expose .value; histograms expose .sum/.count.
                    value = getattr(dp, "value", None)
                    if value is None:
                        value = getattr(dp, "sum", None)
                    pts.append((dict(dp.attributes), value, dp))
                out.setdefault(metric.name, []).extend(pts)
    return out


@pytest.fixture()
def metrics_enabled(monkeypatch):
    """Enable OTel metrics with an InMemoryMetricReader-backed MeterProvider.

    OTel pins the *global* MeterProvider after the first ``set_meter_provider``,
    so per-test isolation can't rely on the global. We instead INJECT a fresh
    provider into ``metrics_otel`` (the documented test seam) and let
    ``_ensure_instruments`` build the real instruments against it — so the
    production code path is exercised while each test gets a clean reader.
    """
    pytest.importorskip("opentelemetry.sdk.metrics")
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader
    from server.observability import metrics_otel

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.test:4317")

    reader = InMemoryMetricReader()
    metrics_otel._reset_for_tests()
    # Inject the provider seam: _ensure_instruments() respects an already-set
    # _meter_provider and builds its instruments against it (no global clobber).
    metrics_otel._meter_provider = MeterProvider(metric_readers=[reader])
    try:
        yield reader, metrics_otel
    finally:
        metrics_otel._reset_for_tests()


# ── enabled: instruments recorded with the right attrs + values ──────────────


def test_record_two_workers_records_three_instruments(metrics_enabled):
    reader, metrics_otel = metrics_enabled
    workers = [
        _worker("claude", "claude-sonnet-4-6", in_t=300, out_t=40, cost=0.30, dur=1500),
        _worker("ollama", "ollama:llama3", in_t=500, out_t=60, cost=0.0, dur=2200),
    ]
    metrics_otel.record_worker_metrics_batch(workers)

    collected = _collect(reader)
    assert "gen_ai.input_tokens" in collected
    assert "gen_ai.output_tokens" in collected
    assert "gen_ai.cost_usd" in collected
    assert "worker.duration_ms" in collected

    # Input tokens by provider/model/phase.
    in_by_provider = {
        a["provider"]: v for a, v, _ in collected["gen_ai.input_tokens"]
    }
    assert in_by_provider == {"claude": 300, "ollama": 500}

    out_by_provider = {
        a["provider"]: v for a, v, _ in collected["gen_ai.output_tokens"]
    }
    assert out_by_provider == {"claude": 40, "ollama": 60}

    # Cost: claude 0.30, ollama 0.0 (local → free, but the series exists).
    cost_by_provider = {
        a["provider"]: v for a, v, _ in collected["gen_ai.cost_usd"]
    }
    assert cost_by_provider["claude"] == pytest.approx(0.30)
    assert cost_by_provider["ollama"] == pytest.approx(0.0)

    # Attributes are exactly the bounded-cardinality set — no worker/task ids.
    for attrs, _, _ in collected["gen_ai.input_tokens"]:
        assert set(attrs) == {"provider", "model", "phase"}
        assert "worker_id" not in attrs
        assert "task_id" not in attrs

    # Duration histogram captured both workers.
    dur_attrs = {a["provider"] for a, _, _ in collected["worker.duration_ms"]}
    assert dur_attrs == {"claude", "ollama"}


def test_model_attribute_present(metrics_enabled):
    reader, metrics_otel = metrics_enabled
    metrics_otel.record_worker_metrics(
        _worker("claude", "claude-sonnet-4-6", in_t=10, out_t=2, cost=0.01, dur=100)
    )
    collected = _collect(reader)
    models = {a["model"] for a, _, _ in collected["gen_ai.input_tokens"]}
    assert models == {"claude-sonnet-4-6"}


def test_missing_provider_buckets_unknown(metrics_enabled):
    reader, metrics_otel = metrics_enabled
    metrics_otel.record_worker_metrics(
        {"input_tokens": 5, "output_tokens": 1, "cost_usd": 0.0, "duration_ms": 0}
    )
    collected = _collect(reader)
    attrs = collected["gen_ai.input_tokens"][0][0]
    assert attrs == {"provider": "unknown", "model": "unknown", "phase": "unknown"}


# ── disabled: complete no-op ─────────────────────────────────────────────────


def test_record_is_noop_when_otel_disabled(monkeypatch):
    """ABSOLUTE CONSTRAINT: with no OTEL endpoint, recording is a complete
    no-op — no error, no instruments built, no metrics."""
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    from server.observability import metrics_otel

    metrics_otel._reset_for_tests()
    try:
        # Must not raise, and must not build any instruments.
        metrics_otel.record_worker_metrics(
            _worker("claude", "x", in_t=10, out_t=2, cost=0.01, dur=100)
        )
        metrics_otel.record_worker_metrics_batch(
            [_worker("ollama", "y", in_t=5, out_t=1, cost=0.0, dur=10)]
        )
        assert metrics_otel._input_tokens_counter is None
        assert metrics_otel._meter_provider is None
    finally:
        metrics_otel._reset_for_tests()


def test_batch_noop_on_empty(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.test:4317")
    from server.observability import metrics_otel

    metrics_otel._reset_for_tests()
    try:
        metrics_otel.record_worker_metrics_batch(None)
        metrics_otel.record_worker_metrics_batch([])
        # Nothing built — short-circuited before init.
        assert metrics_otel._input_tokens_counter is None
    finally:
        metrics_otel._reset_for_tests()


# ── #45 P2: budget.exceeded counter (soft budget alert) ──────────────────────


def test_budget_exceeded_counter_emitted_when_over(metrics_enabled):
    """Budget set + spent > limit → the budget.exceeded counter increments 1
    with the bounded {provider:"aggregate"} attribute and NO task_id label."""
    reader, metrics_otel = metrics_enabled
    metrics_otel.record_budget_exceeded()

    collected = _collect(reader)
    assert "budget.exceeded" in collected
    pts = collected["budget.exceeded"]
    total = sum(v for _, v, _ in pts)
    assert total == 1
    for attrs, _, _ in pts:
        assert attrs == {"provider": "aggregate"}
        assert "task_id" not in attrs
        assert "worker_id" not in attrs
        assert "correlation_key" not in attrs


def test_budget_exceeded_counter_noop_when_otel_disabled(monkeypatch):
    """ABSOLUTE: with no OTEL endpoint, record_budget_exceeded is a complete
    no-op — no error, no instruments built, no metrics."""
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    from server.observability import metrics_otel

    metrics_otel._reset_for_tests()
    try:
        metrics_otel.record_budget_exceeded()  # must not raise
        assert metrics_otel._budget_exceeded_counter is None
        assert metrics_otel._meter_provider is None
    finally:
        metrics_otel._reset_for_tests()


def test_completion_over_budget_emits_counter_under_does_not(metrics_enabled, tmp_path):
    """End-to-end through the completion path with the in-memory reader:
    over-budget read_usage fires budget.exceeded; under-budget does not."""
    import json

    reader, metrics_otel = metrics_enabled
    from server.services import completion

    # Over budget: spent 1.25 > limit 1.00 → counter fires once.
    over = tmp_path / "over"
    over.mkdir()
    (over / "task_metadata.json").write_text(json.dumps({"budgetUsd": 1.00}))
    (over / "token_usage.json").write_text(json.dumps({
        "totalInputTokens": 2400, "outputTokens": 100, "totalTokens": 2500,
        "totalCostUsd": 1.25, "model": "claude-sonnet-4-6",
    }))
    usage = completion.read_usage(over)
    assert usage["budget"]["exceeded"] is True

    collected = _collect(reader)
    assert sum(v for _, v, _ in collected.get("budget.exceeded", [])) == 1

    # Under budget: spent 0.50 < limit 5.00 → no further increment.
    under = tmp_path / "under"
    under.mkdir()
    (under / "task_metadata.json").write_text(json.dumps({"budgetUsd": 5.00}))
    (under / "token_usage.json").write_text(json.dumps({
        "totalInputTokens": 100, "outputTokens": 10, "totalTokens": 110,
        "totalCostUsd": 0.50, "model": "claude-sonnet-4-6",
    }))
    usage2 = completion.read_usage(under)
    assert usage2["budget"]["exceeded"] is False

    collected2 = _collect(reader)
    # Still exactly 1 — the under-budget task did not increment the counter.
    assert sum(v for _, v, _ in collected2.get("budget.exceeded", [])) == 1


# ── _new_traceparent: active-span linkage + fallback ─────────────────────────


def test_new_traceparent_fallback_is_synthetic_when_no_span(monkeypatch):
    """No active span → synthetic random traceparent (old behavior)."""
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    from server.services.completion import _new_traceparent

    tp = _new_traceparent()
    assert _TRACEPARENT_RE.match(tp), tp
    # Two calls differ — freshly rooted each time.
    assert _new_traceparent() != _new_traceparent()


def test_new_traceparent_uses_active_span_context(monkeypatch):
    """An active span → its trace/span id is reused so the event links in."""
    pytest.importorskip("opentelemetry.sdk.trace")
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider

    trace.set_tracer_provider(TracerProvider())
    tracer = trace.get_tracer("test")

    from server.services.completion import _new_traceparent

    with tracer.start_as_current_span("req") as span:
        ctx = span.get_span_context()
        expected = (
            f"00-{ctx.trace_id:032x}-{ctx.span_id:016x}-{ctx.trace_flags:02x}"
        )
        tp = _new_traceparent()
        assert tp == expected
        # The traceparent carries the ACTIVE trace id, not a random one.
        assert tp.split("-")[1] == f"{ctx.trace_id:032x}"
