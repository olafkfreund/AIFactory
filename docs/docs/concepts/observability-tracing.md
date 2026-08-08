---
title: Distributed tracing (OpenTelemetry)
sidebar_position: 11
---

# Distributed tracing (OpenTelemetry)

> *Answer "where did this request spend its time?" with one Tempo / Jaeger / Datadog query. Opt-in, failure-safe, off by default.*

When a portal request triggers a chain — HTTP → DB → agent subprocess → MCP server call → outbound webhook — the AIFactory web-server emits OpenTelemetry spans that link every hop into a single trace. Operators on call can pull up one trace ID and see the full causal graph, including the agent's own work.

When you haven't pointed AIFactory at a collector, the tracing layer is a near-zero-cost no-op: spans build in memory and drop at export. No I/O. No observable cost. Flip the toggle when you have a collector reachable.

## When you need this

You want tracing when **any** of these apply:

- Diagnosing latency: "every dashboard page is slow on Mondays — which dependency is it?"
- Debugging cross-pod issues in multi-replica deployments (an event published on pod A, dispatched on pod B).
- Auditing the agent-task lifecycle: which phase took how long? Which MCP call dominated coding time?
- Operating a managed deployment with a contractual latency SLO.

You **don't** need it for:

- Laptop installs / dev sessions (the no-export default is the right choice).
- Single-replica production where you already have Prometheus + structured logs.
- Sub-1-rps workloads where one slow request is easier to debug by reading the log line directly.

## What's in scope (and what's not)

| Aspect | v1.1 status |
|--------|-------------|
| HTTP request spans (FastAPI auto-instrumentation) | Supported |
| DB call spans (SQLAlchemy + asyncpg) | Supported |
| Outbound httpx spans | Supported |
| Redis pub/sub spans | Supported |
| Agent phase spans (`task:phase:coding`, etc.) | Supported |
| Cross-replica trace continuity (Redis envelope carries `traceparent`) | Supported |
| Agent-subprocess trace continuity (`TRACEPARENT` env var) | Supported (context only — agent doesn't export) |
| Per-worker OTel metrics (`gen_ai.*`, `worker.duration_ms`) from the web-server | Supported (#567) |
| Sampling configuration (ParentBased + ratio) | Supported |
| Trace-aware logs (request_id = trace_id when in span) | Supported |
| Per-MCP-server spans inside the agent | Not yet (v1.2 — needs SDK changes) |
| Custom OTel processors (TailSampling, etc.) | Not yet (set via OTel SDK env vars) |

## How it works

```
Browser ─HTTP─▶ web-server pod A ─AsyncPG─▶ Postgres
                    │
                    ├─Redis publish─▶ Redis ─Redis subscribe─▶ web-server pod B
                    │
                    └─subprocess spawn (TRACEPARENT env)─▶ agent process
                                                                │
                                                                └─httpx, MCP calls...
```

Every box opens its own span; the trace ID flows through every arrow. Tempo / Jaeger / Datadog stitch them into a single waterfall view.

The three propagation boundaries:

1. **In-process** (web-server → DB / httpx / redis): OTel auto-instrumentation handles it.
2. **Cross-replica** (web-server pod A → pod B via Redis pub/sub): AIFactory adds a `trace.traceparent` field to the Redis envelope; the receiving pod re-attaches the parent context before dispatching the event locally. Backward-compatible — old envelopes without the field still dispatch normally.
3. **Cross-process** (web-server → agent subprocess): AIFactory injects `TRACEPARENT` into the subprocess environment via `make_subprocess_env`. The agent's `init_agent_tracing()` (in `apps/backend/core/job_tracing.py`, the hub canonical vendored here — Factory#638) extracts it on startup and attaches the parent context so the agent's logs and metrics carry the originating trace ID.

## Turning it on

The `otel:` block in `values.yaml` controls everything. Minimal config:

```yaml
otel:
  enabled: true
  endpoint: http://tempo.observability.svc:4317
```

That's it. The web-server picks up the endpoint at lifespan startup, installs the OTLP exporter, and starts emitting spans.

### Full configuration

```yaml
otel:
  enabled: true
  endpoint: http://tempo.observability.svc:4317
  protocol: grpc                    # or http/protobuf
  serviceName: aifactory-prod-eu    # default: aifactory-web
  samplingRatio: 0.1                # 10% of root spans
  headersSecretName: tempo-headers  # for vendor auth (Honeycomb / Datadog / NewRelic)
```

The `headersSecretName` references a Kubernetes Secret with the key `OTEL_EXPORTER_OTLP_HEADERS`. Use it for vendor APIs that need a token:

```bash
kubectl create secret generic tempo-headers \
  --from-literal=OTEL_EXPORTER_OTLP_HEADERS="api-key=hc_xxxxx,team=infra" \
  -n aifactory
```

### Validators

The chart fails `helm template` with a clear message when:

- `otel.enabled=true` but `otel.endpoint` is empty.
- `otel.headersSecretName` is set but `otel.enabled` is false (operator typo trap — the Secret would never be consumed).

### Failure-safe contract

If the collector goes down mid-request, the OTel SDK's own machinery logs a WARNING and drops spans. **The calling code path never crashes.** This is a hard contract: AIFactory wraps every integration point in `try/except` so a broken tracer can't break the app.

Verify it yourself: stop the OTLP collector during a load test, then check that:
- The application's `/api/health` keeps returning 200.
- Logs grow with `otel.exporter` WARNING lines (not errors).
- No span data shows up in your backend during the outage.
- Span data resumes immediately once the collector is back.

## Trace-aware logs

When a request hits FastAPI, the OTel instrumentor opens a root span before AIFactory's middleware runs. AIFactory's `CorrelationIdMiddleware` then sources its `request_id` from the active trace ID — so every log line carries the same 32-hex identifier that shows up in the trace backend.

Resolution order:
1. Client-provided `X-Request-ID` (operators correlating across services with their own IDs always win).
2. Active OTel trace ID (when a span is active).
3. Fresh UUID (fallback when neither exists).

In Tempo, click any span → "View logs" jumps straight to the matching log lines. The other direction works too: paste a trace ID from a log line into Tempo's search bar to pull up the full waterfall.

## Sampling guide

| Workload shape | Recommended `samplingRatio` |
|----------------|------------------------------|
| Dev / staging | `1.0` (sample everything) |
| Low-traffic production (< 1 rps) | `1.0` |
| Medium production (1–50 rps) | `0.1` (10%) |
| High production (50+ rps) | `0.01` (1%) — adjust per cost budget |
| Performance test "no-export" baseline | `0.0` (constructs spans, exports none) |

The sampler is `ParentBased(TraceIdRatioBased(ratio))`: children **always** inherit the parent's decision, so a sampled request stays end-to-end visible across the agent / MCP / Redis hops. Only root spans (those without an inherited parent) go through the ratio decision.

## Per-worker metrics (#567)

When a parallel build completes, the web-server emits OpenTelemetry **metrics** for
each worker — alongside the spans above and gated by the same `otel.enabled`
toggle (a no-op when OTel is off):

| Instrument | Meaning |
|------------|---------|
| `gen_ai.input_tokens` | Input tokens consumed by the worker |
| `gen_ai.output_tokens` | Output tokens produced by the worker |
| `gen_ai.cost_usd` | Worker spend in USD |
| `worker.duration_ms` | Worker wall-clock duration |
| `budget.exceeded` | Counter — fires when a build's spend crosses its configured budget (observe-only; never aborts the build) |

Every instrument is tagged `{provider, model, phase}` and **nothing else** — in
particular **never `task_id`**. Provider, model and phase are small closed sets, so
the series stay cheap regardless of how many builds run; per-task labels would blow
up cardinality and are deliberately excluded.

These metrics are emitted **from the web-server, not the agent** — agent
subprocesses inherit the trace context but don't run their own exporter, so the
web-server (which already owns the OTel SDK lifecycle and receives the completion
data) is the single emission point. The same change also fixed the
completion-event `traceparent` to link to the real active span, so a build's
metrics and its trace line up in your backend. The matching per-worker token/cost
breakdown is also written to `token_usage.json` and carried on the v1.3 completion
event — see [Task observability panels](./observability-panels.md).

## What's not yet supported

- **Custom processors** (TailSamplingProcessor, attribute filters, etc.) — set via the OTel SDK's standard env vars (`OTEL_PROCESSOR_BSP_*`, `OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT`, etc.) which the Python SDK picks up automatically. AIFactory doesn't wrap these.
- **Logs** signal — not yet emitted over OTLP; the structlog stack covers it (logs stay trace-aware via the shared trace ID). The **Metrics** signal now has a first set of OTel instruments (see below).
- **Per-MCP-server spans inside the agent** — the agent inherits the parent trace context (so the trace ID is consistent), but the agent doesn't itself export spans for individual MCP calls in v1.1. The web-server's outbound `httpx` calls to MCP servers DO show up.

## See also

- [Multi-replica deployment](./multi-replica.md) — how Redis pub/sub fans out events (the `traceparent` envelope field is added on top).
- [GitHub issue #42](https://github.com/olafkfreund/AIFactory/issues/42) — the original design doc.
- Design doc in-repo: `docs/plans/2026-05-28-otel-tracing-design.md`.
