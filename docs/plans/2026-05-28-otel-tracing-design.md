# Design: OpenTelemetry distributed tracing

> Epic [#35](https://github.com/olafkfreund/AIFactory/issues/35) child [#42](https://github.com/olafkfreund/AIFactory/issues/42). ~1 week effort. Smallest tractable Enterprise v1.1 child.

## Summary

Add end-to-end distributed tracing so operators can answer "where did this request spend its time?" with one Tempo / Jaeger / Datadog query. Auto-instrumentation covers FastAPI / SQLAlchemy / asyncpg / httpx / redis-py for free; manual spans wrap the agent-task phase lifecycle. Trace context propagates across the three boundaries that matter — HTTP→DB (auto), HTTP→Redis pub/sub envelope (additive `trace` field), and web-server→agent subprocess (`TRACEPARENT` env var) — so a single trace covers the request through to the LLM call.

Disabled by default. `OTEL_EXPORTER_OTLP_ENDPOINT` unset → no exporter installed, auto-instrumentation builds spans in memory at near-zero cost and drops at export time. Setting the env (or `otel.enabled=true` in Helm) flips on exporting.

## Decisions locked

| Decision | Choice | Why |
|---|---|---|
| Span scope for agent-task lifecycle | Per phase (planning / coding / review_pending / completed) | Matches the four phase transitions already used for #40 workspace snapshots + WS broadcasts. ~4 spans per task. Reuses existing mental model; per-LLM-call spans appear inside via httpx auto-instrumentation. |
| `correlation_id` ↔ `trace_id` relationship | Keep both; `correlation_id` mirrors `trace_id` when in a span, falls back to UUID otherwise | Zero breaking changes for log-grep workflows that filter on `X-Request-ID` / `request_id`. New traces correlatable end-to-end. Best-of-both. |
| CI e2e trace test | `InMemorySpanExporter` — in-process span capture + assertions | Catches the bugs that matter (context propagation, attribute names, span hierarchy) without provisioning a Tempo container. The issue text said "visible in Tempo" but in-memory proves the same correctness. |
| Auto-instrumentation surface | FastAPI + SQLAlchemy + asyncpg + httpx + redis-py | Exactly the list in the issue. |
| OTLP exporter target | Typed Helm `otel:` block with `endpoint` / `protocol` / `serviceName` / `samplingRatio` / `headersSecretName` | Matches `redis` / `workspaces.storage` / `mcpCredentials` patterns. Required-validator catches misconfiguration at helm template time. |
| Sampler | `ParentBased(rate)` with `rate` from `otel.samplingRatio` | Default 1.0 for pilot. Operators dial down on cost-sensitive SaaS (Datadog, Honeycomb). |
| Trace context propagation across boundaries | HTTP→DB auto; HTTP→Redis envelope additive `trace` field; web-server→subprocess `TRACEPARENT` env | Three boundaries, three standard OTel propagation patterns. No new wire formats. |
| Failure-safe contract | OTLP unreachable → log WARNING, drop spans, app continues | Matches `audit_service` + `workspace_store` patterns. |
| Optional-by-default | `OTEL_EXPORTER_OTLP_ENDPOINT` unset → no exporter | Same model as `REDIS_URL` / `WORKSPACE_S3_URI_BASE`. Laptop installs unaffected. |
| Metrics + logs via OTel | Out of scope for v1.1 | Prometheus stays as-today; structlog stays as-today. v1.1 is traces-only. |

## Architecture

```
HTTP request (with optional traceparent header from caller)
        │
        ▼
   CorrelationIdMiddleware
        │   (reads OTel current span if present → sets request_id = trace_id;
        │    otherwise generates UUID → still set request_id)
        ▼
   FastAPI auto-instrumentation  ──► creates root span "POST /api/tasks/..."
        │
        ├──► SQLAlchemy auto-instrumentation ──► "SELECT ..." child span
        ├──► httpx auto-instrumentation     ──► "POST api.anthropic.com" child span
        ├──► redis auto-instrumentation     ──► "PUBLISH aifactory:events" child span
        │
        └──► Manual span "task:phase:<name>"  (one per phase transition)
                │
                ▼
            make_subprocess_env(env) injects TRACEPARENT
                │
                ▼
            agent subprocess (separate Python process)
                │
                └──► its own OTel SDK seeds from TRACEPARENT
                     ──► creates "agent.coder.run" child span
                         ──► httpx auto-instr ──► "POST api.anthropic.com" child span
```

Agents NEVER see OTel SDK state from the parent process — they only get a `TRACEPARENT` env var. The agent's own OTel init (a tiny snippet) reads that to seed its root context. This keeps the subprocess boundary clean and lets agents be traced independently in CLI-spawned runs.

## Modules

| Module | Change |
|---|---|
| `apps/web-server/server/observability/tracing.py` | **NEW** — `init_tracing()`, `task_phase_span()`, `get_current_traceparent()`, OTLP exporter setup, auto-instrumentation setup |
| `apps/web-server/server/observability/correlation_id.py` | Extend `CorrelationIdMiddleware.dispatch` to source `request_id` from current span's `trace_id` when present |
| `apps/web-server/server/main.py` | Call `tracing.init_tracing()` in app lifespan |
| `apps/web-server/server/utils/subprocess_env.py` | Inject `TRACEPARENT` env var when called inside a span |
| `apps/web-server/server/websockets/event_bus.py` | `_serialize_envelope`: add `trace` field when in span; `_parse_envelope`: extract; `_dispatch_envelope`: create child span when present |
| `apps/web-server/server/services/agent_service.py` | Wrap each phase boundary in `task_phase_span(task_id, phase)` |
| `apps/web-server/requirements.txt` | Adds `opentelemetry-{api,sdk,exporter-otlp,instrumentation-fastapi,instrumentation-sqlalchemy,instrumentation-asyncpg,instrumentation-httpx,instrumentation-redis}` |
| `charts/aifactory/values.yaml` | New `otel:` block |
| `charts/aifactory/templates/deployment.yaml` | Conditional `OTEL_*` env injection + validator |
| `tests/test_tracing.py` | **NEW** — InMemorySpanExporter unit tests |
| `tests/test_tracing_e2e.py` | **NEW** — InMemorySpanExporter cross-component propagation test |
| `tests/helm/test_otel_toggle.py` | **NEW** — Helm chart toggle/validator tests |
| `apps/backend/core/tracing_bootstrap.py` (new) | Reads `TRACEPARENT` env on agent boot if `opentelemetry` is importable. Calls `init_tracing()` with the same exporter env reads, then `.instrument()` for `httpx` (the one the LLM call uses). Import-guarded so the agent runs fine without OTel installed (CLI users + dev installs). OTel deps go in the web-server `requirements.txt` only; the agent backend's `requirements.txt` is unchanged in v1.1 — when running inside the same image as the web-server, the SDK is already importable. |

## Module surface — `tracing.py`

```python
# Module-level state — initialized once at startup
_tracer_provider: TracerProvider | None = None


def init_tracing() -> None:
    """Idempotent. Called from main.py app lifespan.

    - Reads OTEL_EXPORTER_OTLP_ENDPOINT; if unset → installs a no-op
      tracer provider (spans still build in memory, get dropped at
      export). Zero observable cost.
    - When endpoint is set → installs OTLPSpanExporter, configures
      BatchSpanProcessor, wires the W3C TraceContext propagator.
    - Calls .instrument() for FastAPI / SQLAlchemy / asyncpg /
      httpx / redis.
    - Idempotent: second call no-ops (auto-instrumentation libs
      already handle double-init gracefully).
    """


@contextmanager
def task_phase_span(task_id: str, phase: str) -> Generator[Span]:
    """Start a span named ``task:phase:{phase}`` with attributes
    ``task.id`` + ``task.phase``.

    Becomes a child of the surrounding HTTP request span when one
    exists, otherwise becomes a root span. Used by agent_service at
    the 4 phase transitions (planning / coding / review_pending /
    completed).
    """


def get_current_traceparent() -> str | None:
    """Return the W3C traceparent header string for the current
    span, or None when no span is active.

    Used by ``subprocess_env`` to inject ``TRACEPARENT`` and by
    ``event_bus`` to inject into the envelope's ``trace`` field.
    """
```

## Settings (`config.py`)

The OTel SDK reads its config from standard `OTEL_*` env vars natively (`OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_SERVICE_NAME`, etc.). No new fields needed in `config.py` — the Helm chart sets the envs directly.

## Helm chart additions

```yaml
otel:
  # Master toggle. When false (default), no OTel envs are injected
  # and the app starts with auto-instrumentation enabled but a
  # no-op exporter — spans build in memory cheaply and get dropped.
  # Flip to true to start exporting to a collector.
  enabled: false

  # OTLP collector endpoint. REQUIRED when enabled=true.
  # Examples:
  #   http://tempo.tempo.svc:4317  (in-cluster Tempo, gRPC)
  #   http://otel-collector.observability.svc:4318  (collector, HTTP)
  #   https://api.honeycomb.io      (SaaS, requires headersSecretName)
  #   https://otlp.nr-data.net      (New Relic, requires headersSecretName)
  endpoint: ""

  # grpc (default — port 4317) or http/protobuf (port 4318).
  protocol: "grpc"

  # service.name span attribute. Override per-deployment when
  # operators run multiple AIFactory instances against one collector.
  serviceName: "aifactory-web"

  # ParentBased sampler's root-span rate. 1.0 = always sample roots.
  # Dial down (0.1, 0.01) for cost control on SaaS collectors.
  # Child spans inherit their parent's sample decision regardless.
  samplingRatio: 1.0

  # Secret with OTLP headers for SaaS-vendor auth (e.g.
  # "authorization: Api-Key xxx" for Datadog, "x-honeycomb-team: xxx"
  # for Honeycomb). The Secret key MUST be "OTEL_EXPORTER_OTLP_HEADERS"
  # in the standard W3C-comma-separated format. Optional.
  headersSecretName: ""
```

**Render-time validators:**
- `otel.enabled=true` + empty `endpoint` → fails with "otel.enabled=true requires otel.endpoint (e.g. http://tempo.tempo.svc:4317)"
- `otel.enabled=true` + `protocol` not in `{grpc, http/protobuf}` → fails
- `headersSecretName` set + `otel.enabled=false` → fails with "otel.headersSecretName has no effect without otel.enabled=true" (catches the silent-no-op operator footgun)

**Env injection on the deployment** (when `otel.enabled=true`):

```yaml
- name: OTEL_EXPORTER_OTLP_ENDPOINT
  value: {{ required "otel.enabled=true requires otel.endpoint" .Values.otel.endpoint | quote }}
- name: OTEL_EXPORTER_OTLP_PROTOCOL
  value: {{ .Values.otel.protocol | quote }}
- name: OTEL_SERVICE_NAME
  value: {{ .Values.otel.serviceName | quote }}
- name: OTEL_TRACES_SAMPLER
  value: "parentbased_traceidratio"
- name: OTEL_TRACES_SAMPLER_ARG
  value: {{ .Values.otel.samplingRatio | quote }}
# When headersSecretName set
- name: OTEL_EXPORTER_OTLP_HEADERS
  valueFrom:
    secretKeyRef:
      name: {{ .Values.otel.headersSecretName }}
      key: OTEL_EXPORTER_OTLP_HEADERS
```

## Event-bus envelope (additive v1 field)

```json
{
  "v": 1,
  "source": "<replica UUID>",
  "scope": {"kind": "broadcast"},
  "type": "task:log",
  "payload": { ... },
  "trace": {
    "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
    "tracestate": ""
  }
}
```

- `trace` field is **optional + additive**. Older subscribers (from PRs #171/#172) ignore it — they key only on `kind`/`type`/`payload`/`v`.
- Subscribers built with this PR check for `trace.traceparent`; if present, extract via the W3C propagator + create a child span named `event_bus.deliver` so the cross-replica WS delivery appears in the parent trace.
- Backward compatibility: no envelope version bump. Rolling deploy is safe — new replicas write the field, old replicas ignore.

## Integration hooks

**Middleware ordering:** the OTel FastAPI instrumentor installs at the ASGI layer (via `FastAPIInstrumentor.instrument_app()`), which runs OUTSIDE the user middleware stack — so by the time `CorrelationIdMiddleware.dispatch` executes, the OTel root span for the HTTP request already exists and `trace.get_current_span()` returns it. No middleware re-ordering needed; `init_tracing()` just has to run before `app.add_middleware(CorrelationIdMiddleware)` in `main.py`. Both happen in `lifespan` startup so ordering is enforced by the existing code.

**`CorrelationIdMiddleware` (correlation_id.py):**

```python
async def dispatch(self, request, call_next):
    # NEW: if there's an active OTel span (FastAPI auto-instr started one
    # already), source request_id from its trace_id. Otherwise fall back
    # to the existing header-or-uuid logic.
    from opentelemetry import trace
    span = trace.get_current_span()
    span_ctx = span.get_span_context() if span else None
    if span_ctx and span_ctx.is_valid:
        rid = format(span_ctx.trace_id, "032x")
    else:
        rid = request.headers.get(CORRELATION_ID_HEADER) or str(uuid.uuid4())
    token = _correlation_id.set(rid)
    try:
        response = await call_next(request)
    finally:
        _correlation_id.reset(token)
    response.headers[CORRELATION_ID_HEADER] = rid
    return response
```

**`make_subprocess_env` (subprocess_env.py):**

```python
def make_subprocess_env(...) -> dict[str, str]:
    env = {...existing logic...}
    # NEW: inject TRACEPARENT so the agent subprocess's OTel SDK
    # seeds from the current span. Absent when no span active.
    from .observability.tracing import get_current_traceparent
    tp = get_current_traceparent()
    if tp:
        env["TRACEPARENT"] = tp
    return env
```

**`event_bus._serialize_envelope`:**

```python
def _serialize_envelope(scope, event_type, payload) -> str:
    envelope = {...existing fields...}
    # NEW: inject trace context when in a span
    tp = get_current_traceparent()
    if tp:
        envelope["trace"] = {"traceparent": tp, "tracestate": ""}
    return json.dumps(envelope)
```

**`event_bus._dispatch_envelope`** extracts the envelope's `trace` field, uses the W3C propagator to extract a `Context`, then opens a child span named `event_bus.deliver` for the local delivery work.

**`agent_service._safe_emit_task_status`** wraps the phase boundary work in `task_phase_span(task_id, phase)`. The phase span carries `task.id` + `task.phase` attributes for filterability in Tempo / Jaeger.

## Error handling

| Failure | Behavior |
|---|---|
| `otel.enabled=false` (default) | No exporter. Auto-instrumentation builds spans in memory, drops at export. Zero observable cost. |
| `otel.enabled=true` but collector unreachable | App starts. SDK logs ERROR; subsequent export attempts retry with backoff per OTel SDK defaults. App behavior unaffected. |
| Collector drops connection mid-session | SDK queues + retries; spans dropped if queue overflows (configurable). DEBUG log on drop; never raises. |
| `headersSecretName` references a Secret missing the expected key | SDK init logs "header missing" error; tracing degrades to "spans built but not exported". App keeps running. |
| Sampler rate misconfigured (negative number, > 1.0) | SDK falls back to AlwaysOff with warning. Spans built but never exported. |
| Auto-instrumentation conflict (e.g. SQLAlchemy double-instrumented) | OTel handles gracefully (no-op). Warned once per process. |
| Subprocess receives malformed `TRACEPARENT` | Standard W3C propagator drops + starts a fresh root span. Child agent's spans become a separate trace; no error. |
| Helm install with `otel.enabled=true` and no `endpoint` | helm template fails with the required-validator message. |

## Testing

**Unit — `tests/test_tracing.py`** (uses `InMemorySpanExporter`, no network):

> **Test fixture note:** the in-memory tests use `SimpleSpanProcessor` (not the production-default `BatchSpanProcessor`) so span assertions don't race with the async batch flush. Use `force_flush()` before reading the exporter if BatchSpanProcessor is required for any specific test.

- `init_tracing()` is idempotent
- `init_tracing()` with `OTEL_EXPORTER_OTLP_ENDPOINT` unset → no-op exporter; spans still build
- `init_tracing()` with the env set → OTLPSpanExporter installed; spans flush on close
- `task_phase_span` creates `task:phase:<name>` with `task.id` + `task.phase` attributes
- When called inside a request: phase span is a child of the FastAPI request span
- `get_current_traceparent()` returns valid W3C string in a span; None outside
- `CorrelationIdMiddleware` sets `request_id` = `trace_id` when a span exists; UUID fallback otherwise
- `make_subprocess_env` injects `TRACEPARENT` matching the current span; absent when no span
- `event_bus._serialize_envelope` includes `trace` field when in span; omitted otherwise
- `event_bus._parse_envelope` extracts `trace.traceparent` when present; tolerates absence (older envelopes)

**Integration-style — `tests/test_tracing_e2e.py`** (still InMemorySpanExporter):

- TestClient request → assert span tree: root `POST /api/...` → child `task:phase:planning` → child `subprocess.spawn` → asserted `TRACEPARENT` on the spawned env
- TestClient request that fires a Redis broadcast → assert the published envelope JSON carries `trace.traceparent` matching the request's trace_id
- Receive a Redis envelope with a `trace` field → assert a child span `event_bus.deliver` is created with parent matching the envelope's traceparent

**Helm — `tests/helm/test_otel_toggle.py`:**

- Off → no `OTEL_*` env vars on container
- On + endpoint → all 5 `OTEL_*` envs render
- On + `headersSecretName` → `OTEL_EXPORTER_OTLP_HEADERS` via `valueFrom.secretKeyRef`
- Validator: `enabled=true` without endpoint → fails with expected message
- `samplingRatio` renders as string (OTel SDK quirk: SAMPLER_ARG must be string)
- `samplingRatio: 0.0` accepted (operator can disable sampling without disabling the whole SDK; `enabled=true` + `samplingRatio=0.0` is a legitimate config)
- Validator: `headersSecretName` set with `enabled=false` → helm template fails (catches silent-no-op operator footgun)
- Coexists with `redis` + `workspaces.storage` blocks (all three Epic #35 children render together)

## Migration

No data migration. Code path is additive. Existing `correlation_id` semantics preserved (UUID fallback when no span). 65+ existing call sites of `broadcast_event` / `send_to_user` / `send_to_org` unchanged — they automatically get OTel context injected by the bus.

Rolling deploy is safe: old replicas don't read `trace` envelope field (ignore); new replicas write + read it. No breaking changes.

## Out of scope

- **Metrics + logs via OTel** — Prometheus stays (`/metrics` via `prometheus-fastapi-instrumentator`). Structlog stays (JSON-to-stdout, collected by Fluent Bit / Loki). v1.1 is traces-only.
- **OTel Collector deployment** — operators run their own (Tempo, Jaeger, vendor SaaS). Chart doesn't deploy a collector subchart.
- **Agent subprocess full-fidelity tracing** — subprocess gets `TRACEPARENT` + creates one root span for its run; auto-instrumentation of httpx INSIDE the subprocess emits LLM-call spans automatically. Per-tool / per-thought spans are out of scope.
- **Custom resource detectors** — `service.name` + `service.namespace` come from `OTEL_RESOURCE_ATTRIBUTES` env. K8s-attribute auto-detection (pod name, node) is operator-side via the collector's `k8sattributes` processor.
- **Trace-based alerting** — operator concern; chart doesn't ship alert rules.

## Acceptance criteria (PR-close gate)

- [ ] `apps/web-server/server/observability/tracing.py` shipped with the surface above
- [ ] `init_tracing()` called from `main.py` lifespan
- [ ] `CorrelationIdMiddleware` sources `request_id` from `trace_id` when present
- [ ] `make_subprocess_env` injects `TRACEPARENT` when in a span
- [ ] `event_bus` envelope carries `trace` field on publish; subscriber creates child span on receive
- [ ] `agent_service._safe_emit_task_status` wraps phase work in `task_phase_span`
- [ ] Helm chart `otel:` block + validator + helm tests green
- [ ] Unit tests via `InMemorySpanExporter` pass without network
- [ ] Concept doc `docs/docs/concepts/tracing.md` covers operator setup (in-cluster Tempo + SaaS Datadog/Honeycomb examples)
- [ ] Full pytest suite remains 0-fail

## Estimate

~1 week. Likely 1-2 PRs depending on review surface:
- **PR-1:** SDK init + subprocess + middleware + unit tests + requirements
- **PR-2:** Helm + envelope propagation + concept doc + e2e test

## Related

- Parent Epic [#35](https://github.com/olafkfreund/AIFactory/issues/35) — Enterprise v1.1
- Parent issue [#42](https://github.com/olafkfreund/AIFactory/issues/42) — this work
- Sibling specs shipped this session:
  - `docs/plans/2026-05-28-redis-ws-fanout-design.md` (event bus envelope contract this builds on)
  - `docs/plans/2026-05-28-s3-workspaces-design.md` (snapshot model context)
- Epic [#33](https://github.com/olafkfreund/AIFactory/issues/33) (closed P6 work) — structlog + Prometheus surface this extends
