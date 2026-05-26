# Observability runbook

> Audience: SRE / platform teams operating AIFactory v1.0.
> Compliance: SOC2 CC7.2 (monitoring), CIS 16.x (audit log review).
>
> Goal: connect AIFactory to your existing Prometheus + Grafana +
> Loki/ELK stack with minimum custom integration.

## What's exposed

| Surface | Path / Mechanism |
| --- | --- |
| Structured JSON logs | stdout (parseable with `jq`; ship via Vector / Fluent Bit / Promtail) |
| Prometheus metrics | `GET /metrics` (Prometheus exposition format) |
| Liveness / readiness | `GET /api/health` |
| Correlation IDs | `X-Request-ID` request + response header |
| Grafana dashboard | `guides/observability/grafana-aifactory.json` |
| Distributed tracing | **deferred to v1.1** (OpenTelemetry) — documented limitation |

## Structured logs (P6.1)

Every log line is a single JSON object with a stable schema:

```json
{
  "event": "user logged in",
  "level": "info",
  "logger": "server.routes.auth_routes",
  "timestamp": "2026-05-25T19:42:13.187Z",
  "request_id": "8c9d4e3f-0a1b-4c5d-...",
  "user_id": "...",
  "duration_ms": 42
}
```

Field semantics:
  - `timestamp` — ISO-8601 UTC.
  - `level` — `debug` / `info` / `warning` / `error` / `critical`.
  - `logger` — Python module path (e.g. `server.routes.auth_routes`).
  - `event` — the log message.
  - `request_id` — correlation ID (P6.2); present on every line
    emitted inside a request scope. Absent for boot / background-job
    logs.
  - All `kwargs` passed to `logger.info("event", k=v)` are inlined as
    top-level fields.

**Shipping to Loki**:

```yaml
# Promtail snippet — drop into existing config:
- job_name: aifactory
  static_configs:
    - targets: [localhost]
      labels:
        job: aifactory
        __path__: /var/log/pods/*aifactory*/aifactory/*.log
  pipeline_stages:
    - json:
        expressions:
          level: level
          request_id: request_id
    - labels:
        level:
        request_id:
```

**Shipping to ELK**:

Filebeat / Logstash treat each JSON line as a document; no
transformation needed. Configure `decode_json_fields` on the input
to expand the top-level fields into searchable terms.

## Correlation IDs (P6.2)

Every HTTP request carries `X-Request-ID`. If the client doesn't
send one, the middleware auto-generates a UUID4. The response always
echoes the ID back so callers can pair their request with our log
lines + metrics.

Inside the request scope:
  - The ID is bound to `structlog` (every log line includes it).
  - It propagates to outbound `httpx` calls automatically (see
    `install_httpx_propagation` in `server.observability.correlation_id`).
  - Background jobs (`server.jobs.audit_retention`, etc.) inherit
    the parent's ID if invoked from a request scope.

**Tracing a request end-to-end**:

```bash
# Get the correlation ID from your client:
curl -fsSL -H "X-Request-ID: trace-me-abc" https://aifactory/api/health

# Then in Loki / Grafana Explore:
{ job="aifactory" } | json | request_id = "trace-me-abc"
# Every log line for that single request, server-side, in one view.
```

## Prometheus metrics (P6.3 + P6.4)

`/metrics` exposes the standard FastAPI-instrumentator metric set:

| Metric | Type | Labels |
| --- | --- | --- |
| `http_requests_total` | Counter | `handler`, `method`, `status` |
| `http_request_duration_seconds` | Histogram | `handler`, `method`, `status` |
| `http_request_size_bytes` | Summary | `handler`, `method`, `status` |
| `http_response_size_bytes` | Summary | `handler`, `method`, `status` |
| `http_requests_inprogress` | Gauge | (none) |

**Cardinality cap**: `handler` is the FastAPI **route template**, not
the raw request path. So `/api/projects/abc123/tasks` and
`/api/projects/xyz789/tasks` both produce
`handler="/api/projects/{project_id}/tasks"`. Without this, every
new project ID would explode cardinality.

The acceptance test
(`test_handler_label_uses_route_template`) gates this on every PR.

**Excluded paths**: `/metrics` itself + `/api/health`. Including them
would inflate the request-rate panel with health-check traffic.

### Authenticated scrape

Set `METRICS_SCRAPE_TOKEN` to require a bearer:

```bash
# App container env:
METRICS_SCRAPE_TOKEN=$(openssl rand -base64 32)

# Prometheus scrape config (or Helm chart's metrics.scrapeTokenSecret):
- job_name: aifactory
  bearer_token_file: /etc/secrets/aifactory-scrape-token
  static_configs:
    - targets: [aifactory.aifactory.svc.cluster.local]
```

Default (no token): open scrape. NetworkPolicy from P4.3 restricts
the scrape source by namespace, which is the v1.0 baseline.

### ServiceMonitor (Prometheus Operator)

When using `kube-prometheus-stack` or any Prometheus Operator
install, opt into the auto-discovered ServiceMonitor:

```yaml
# values.yaml
metrics:
  serviceMonitor:
    enabled: true
    interval: 30s
  scrapeTokenSecret:
    name: aifactory-metrics  # K8s Secret with key=token
    key: token
```

The chart renders `templates/servicemonitor.yaml` with the bearer
token wired automatically.

## Grafana dashboard (P6.5)

Import `guides/observability/grafana-aifactory.json` via Grafana's
Dashboards → Import. UID: `aifactory-v1`. Schema version: 39
(Grafana 11+).

Panels:
  1. **Request rate (req/s)** — by handler (route template).
  2. **p50 / p99 latency (s)** — by handler.
  3. **Error rate (5xx %)** — single stat with green/yellow/red
     thresholds at 1% / 5%.
  4. **Agent task throughput** — rate of `/api/tasks/*` calls.
  5. **Audit write rate** — POST rate on org audit routes.
  6. **OIDC login success / failure** — by status code on `/callback`.
  7. **Active in-flight requests** — single stat.

The datasource is a template variable so the same JSON imports into
any Prometheus instance.

## OpenTelemetry distributed tracing

**v1.0 limitation**: no built-in OTel exporter. AIFactory's
correlation-ID + structured logs let you cross-correlate manually,
but trace spans / waterfall views require OTel.

**v1.1 plan** (Epic #35): integrate `opentelemetry-instrumentation-
fastapi` + `-sqlalchemy` + `-httpx` with an OTLP exporter
configurable via env. Documented in the [enterprise v1.0 design
spec §3.1.6](../plans/2026-05-24-aifactory-enterprise-v1-design.md).

**Workaround for v1.0**: correlation IDs + log aggregation give you
~80% of the value at the cost of grep instead of waterfall view.

## Troubleshooting

| Symptom | Cause | Resolution |
| --- | --- | --- |
| `/metrics` returns 401 with valid bearer | Token mismatch between app env + Prometheus scrape | `kubectl exec aifactory-... -- env \| grep METRICS_SCRAPE_TOKEN` and compare to the Secret Prometheus reads. |
| Log lines aren't JSON | structlog not configured (called before `configure_structlog`) | Confirm `configure_structlog()` runs in `main.py`'s `lifespan` startup. |
| Cardinality high on `handler` | Custom routes not using route templates | Inspect with `curl /metrics \| grep handler= \| sort -u`; any handler containing `{` is good. Raw IDs = a route registered with raw string concat instead of FastAPI's path templates. |
| Grafana panels show "No data" | Prometheus job label != `aifactory` | Edit the dashboard's `job=` filter to match your scrape config, OR set `job: aifactory` in the scrape config. |
| Request ID not in logs | structlog setup ran before middleware installed | Wire CorrelationIdMiddleware in main.py BEFORE the auth middleware. |

## Related

- [helm-install.md](../deployment/helm-install.md) — chart install.
- [audit-trail.md](audit-trail.md) — audit chain (P5).
- Source: `apps/web-server/server/observability/`,
  `charts/aifactory/templates/servicemonitor.yaml`,
  `guides/observability/grafana-aifactory.json`.
