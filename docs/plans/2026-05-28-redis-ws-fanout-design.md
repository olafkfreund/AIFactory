# Design: Redis pub/sub WebSocket fan-out

> Sub-spec of Epic [#35](https://github.com/olafkfreund/AIFactory/issues/35) child [#40](https://github.com/olafkfreund/AIFactory/issues/40) (half A — the Redis half). The S3 workspace half is a separate spec.

## Summary

Unblock `replicas > 1` for the AIFactory web pod by routing WebSocket broadcasts through a Redis pub/sub bridge. Application code keeps calling the existing `broadcast_event` / `send_to_user` / `send_to_org` functions unchanged — only the internals route through a new event-bus indirection that publishes to Redis when configured and falls back to in-process delivery when not.

Redis remains **optional**: laptop installs, single-replica K8s pilots, and dev environments work unchanged with no Redis configured. Setting `REDIS_URL` enables the pub/sub path. Multi-replica deployments without `REDIS_URL` start successfully but log a warning that cross-replica delivery is disabled.

Terminal WS streams (`/ws/terminal/{id}`) are **out of scope** for this PR. They're stateful subprocess streams that can't be fan-out'd; operators running `replicas > 1` use ingress sticky-cookie annotations on the `/ws/terminal/*` path to pin those connections to the spawning replica. Documented but no app-code change.

## Decisions locked

| Decision | Choice | Why |
|---|---|---|
| Channel granularity | Single global `aifactory:events` | Pilot scale (≤5 replicas, 10s-100s events/sec) is far from any per-task or per-org granularity payoff. YAGNI; can split later without breaking the wire format. |
| Terminal stream strategy | Ingress sticky-cookie on `/ws/terminal/*`; no Redis session table | Terminals own a subprocess on one replica. Cross-replica routing via a session table or app-level proxy is heavy for what `rmux` is in v1. |
| Redis required? | Optional. `REDIS_URL` empty = in-process only | Same pattern as `DATABASE_URL` (defaults to SQLite for laptop). Multi-replica K8s installs flip the toggle. |
| Redis-down behavior | Log + degrade to in-process | Own clients still get own-replica events. Other replicas' clients miss events for the outage window. No buffering, no replay — matches at-most-once WS semantics. |
| Echo suppression | `source_replica_id` UUID in envelope | Publisher also calls `deliver_local` directly so local delivery is unaffected by Redis latency or outages. Subscriber skips messages where `source == self_replica_id`. |
| CI strategy | Python integration test (2 in-process app instances + 1 Redis) | Cheaper than kind, catches the same bugs that matter for this spec. Helm template tests cover chart wiring. |
| HPA | Keep CPU metric; bump defaults from `1/1` to `1/3` | No custom metric work in scope. Per-WS-connection scaling is a separate concern. |

## Architecture

```
agent_service / route handlers / ...
        │  (calls unchanged, ALL 65 existing call sites)
        ▼
   broadcast_event() / send_to_user() / send_to_org()    [ websockets/events.py — thin shims ]
        │
        ▼
   publish_event(scope, type, payload)                    [ NEW: websockets/event_bus.py ]
        │
        ├── deliver_local(scope, type, payload)           ← fires immediately for own replica's clients
        │
        └── if REDIS_URL set:
                redis.publish(REDIS_CHANNEL, envelope)
                                                          ↑
                                                          │
   redis_subscriber_loop()  ◀──── Redis pub/sub ─────────┘
        │
        └── deliver_local(...) IFF envelope.source != self_replica_id
```

**Key property:** local delivery is synchronous and independent of Redis. A Redis outage doesn't block in-replica events from reaching their clients; it only means cross-replica delivery is paused until reconnect.

## Modules

| Module | Change |
|---|---|
| `apps/web-server/server/websockets/event_bus.py` | **NEW** — `publish_event`, `deliver_local`, subscriber lifecycle, `self_replica_id`, `_clients` registry (moved from events.py) |
| `apps/web-server/server/websockets/events.py` | Refactored: three public functions become thin shims over `publish_event`; signatures unchanged; `_clients` registry imported from event_bus |
| `apps/web-server/server/main.py` | App lifespan starts/stops the subscriber when `REDIS_URL` is set |
| `apps/web-server/server/config.py` | Adds `REDIS_URL` and `REDIS_CHANNEL` settings |
| `apps/web-server/requirements.txt` | Adds `redis>=5.0` (asyncio support built in) |
| `tests/requirements-test.txt` | Adds `fakeredis>=2.20` (used by the unit-test fixture) |
| `charts/aifactory/values.yaml` | Adds `redis:` block with `enabled` / `url` / `externalSecretName` / `channel` |
| `charts/aifactory/templates/deployment.yaml` | Injects `REDIS_URL` env from Secret when `redis.enabled=true` |
| `charts/aifactory/templates/_helpers.tpl` | Adds validator that fails template if `redis.enabled=true` without a source |
| `charts/aifactory/values.yaml` autoscaling block | Bumps default `maxReplicas` from `1` to `3`; removes the v1.0 pin comment |

Zero changes to the 65 call sites that already use `broadcast_event` / `send_to_user` / `send_to_org`.

## Data: Redis wire envelope

Each Redis message is a JSON object with this shape. `scope` is a tagged
object — never a bare string — so receivers always discriminate on the
`kind` key without ambiguity.

```json
// broadcast scope — fired by broadcast_event(...)
{
  "v": 1,
  "source": "f0e9d8c7-...",
  "scope": {"kind": "broadcast"},
  "type": "task:log",
  "payload": { "task_id": "001-foo", "line": "..." }
}

// user-scoped — fired by send_to_user(user_id, ...)
{
  "v": 1,
  "source": "f0e9d8c7-...",
  "scope": {"kind": "user", "user_id": "u-42"},
  "type": "notification",
  "payload": { ... }
}

// org-scoped — fired by send_to_org(org_id, ...)
{
  "v": 1,
  "source": "f0e9d8c7-...",
  "scope": {"kind": "org", "org_id": "org-99"},
  "type": "project:status",
  "payload": { ... }
}
```

- `v` — envelope version. Lets us evolve the format without breaking older replicas during rolling deploys. v1 readers tolerate unknown future fields; future readers reject v1 with a warning.
- `source` — UUID generated once at app startup (`uuid.uuid4()`). Used for echo suppression on subscribe; logged at startup so operators can correlate Redis traffic to specific pod instances.
- `scope` — tagged object (`{"kind": ...}` discriminator). Subscriber dispatches on `scope.kind` to the right local-delivery path.
- `type` + `payload` — existing WS message shape, passed through unchanged.

## Interfaces — new module `event_bus.py`

```python
# Module-level state, initialized once at import time
self_replica_id: str = str(uuid.uuid4())

# Tagged-union scope
@dataclass(frozen=True)
class BroadcastScope: pass

@dataclass(frozen=True)
class UserScope: user_id: str

@dataclass(frozen=True)
class OrgScope: org_id: str

Scope = BroadcastScope | UserScope | OrgScope

# Lifecycle — called from main.py app lifespan
async def start_redis_subscriber() -> None
async def stop_redis_subscriber() -> None

# Hot path — called by events.py shims
async def publish_event(scope: Scope, event_type: str, payload: dict) -> None
async def deliver_local(scope: Scope, event_type: str, payload: dict) -> None

# Connection registry (moved from events.py — same shape, new home)
_clients: dict[WebSocket, ConnectedClient]
def register_client(ws: WebSocket, user_info: dict | None) -> ConnectedClient
def unregister_client(ws: WebSocket) -> None
```

## Settings (`config.py`)

```python
REDIS_URL: str = ""                     # empty = in-process only mode
REDIS_CHANNEL: str = "aifactory:events" # configurable for shared Redis instances
```

## Helm chart additions

```yaml
# values.yaml — new block
redis:
  enabled: false
  url: ""                              # inline (for dev/test only; not for prod)
  externalSecretName: ""               # name of an existing Secret with key "REDIS_URL"
  channel: "aifactory:events"
```

**Validator:** template renders an error when `redis.enabled=true` and both `url` and `externalSecretName` are empty. Pattern matches `mcpCredentials` and `remoteControl` validators already in the chart.

**`autoscaling` defaults:** `minReplicas: 1`, `maxReplicas: 3` (was effectively `1/1` in v1.0). Operator can still override either way.

## Lifecycle (startup + shutdown)

```python
# main.py
@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.REDIS_URL:
        await event_bus.start_redis_subscriber()
        logger.info(
            "Redis pub/sub enabled — replica %s subscribed to %s",
            event_bus.self_replica_id, settings.REDIS_CHANNEL,
        )
    else:
        logger.info(
            "Redis pub/sub disabled (REDIS_URL unset) — using in-process broadcasts only"
        )
    yield
    if settings.REDIS_URL:
        await event_bus.stop_redis_subscriber()
```

`start_redis_subscriber` spawns one long-running asyncio task per replica that consumes `pubsub.listen()` and dispatches into `deliver_local`. `stop_redis_subscriber` cancels the task and closes the connection.

## Error handling

| Failure | Behavior |
|---|---|
| `REDIS_URL` set but Redis unreachable at startup | App starts; logs ERROR; subscriber enters reconnect loop with exponential backoff (1s → 30s, capped). Publishes silently fail (logged at DEBUG, not WARNING — would flood). Local delivery unaffected. |
| Subscriber connection drops mid-session | Subscriber logs WARNING, reconnects using the same exponential backoff policy as startup (1s → 30s, capped). Events from other replicas missed during the gap; no replay. |
| Publish call fails (Redis hiccup) | `publish_event` catches, logs DEBUG, continues. Own clients already received the event (local delivery is synchronous and fires first). |
| Malformed envelope received | Subscriber logs WARNING with truncated bytes, skips, continues listening. |
| Envelope `v` field unknown (future format) | Log WARNING, skip. Cross-version rolling deploys keep working as long as new format is additive. |
| Helm install with `redis.enabled=true` but no source | `helm template` fails with `"redis.enabled=true requires either redis.url or redis.externalSecretName"`. |

## Testing

**Unit — `tests/test_event_bus.py`** (fakeredis-aioredis fixture, runs in CI on every PR):

- `publish_event` calls `deliver_local` for own replica regardless of Redis state
- Subscriber dispatches to `deliver_local` when receiving a message with a different `source`
- Subscriber skips messages where `source == self_replica_id` (echo suppression)
- `BroadcastScope` / `UserScope` / `OrgScope` filtering on `deliver_local` matches today's behavior — port the existing tests for `broadcast_event` / `send_to_user` / `send_to_org` to drive through the bus
- Envelope `v` field is always `1`; `v` mismatch on receive → logged + skipped
- Subscriber reconnect loop fires on connection drop (mock the redis client to raise then succeed)

**Integration — `tests/test_redis_ws_fanout_integration.py`** (skip-when-unreachable, mirroring the `TEST_DATABASE_URL` pattern):

- `TEST_REDIS_URL` env (default `redis://localhost:6379/15`) skipped if unreachable
- Two FastAPI app instances built in-process, both pointing at the same Redis
- Open WS client to instance A; fire `broadcast_event` on instance B; assert client receives within 100ms
- Same with `send_to_user` — only the matching user's clients receive
- Same with `send_to_org` — only the matching org's clients receive
- Kill instance B's Redis connection mid-flight; verify A's own clients still get A's locally-fired events (graceful degradation)

**Helm — `tests/helm/test_redis_toggle.py`:**

- Off → no `REDIS_URL` env, no Secret reference, chart renders identically to v1.0
- `redis.enabled=true` + `externalSecretName=my-redis` → `REDIS_URL` env via `valueFrom.secretKeyRef`; env ordering preserved (don't accidentally reshuffle)
- `redis.enabled=true` + `url=redis://...` → `REDIS_URL` env inline
- `redis.enabled=true` with neither → helm template fails with the expected error message
- `autoscaling` defaults render `minReplicas: 1, maxReplicas: 3`

**CI matrix:**

- Add `services: { redis: image: redis:7-alpine }` to the existing `backend (ruff + pytest)` GHA job; integration tests skipped on local dev runs without `TEST_REDIS_URL`, fired automatically in CI.
- Helm tests run in the existing helm-acceptance job — no new infra.

## Migration

No data migration. Code path is additive: existing in-process behavior is preserved when `REDIS_URL` is unset. Rolling deploy is safe — old replicas still work, new replicas with `REDIS_URL` set publish; once all replicas are upgraded, fan-out is fully active.

The `events.py` shim functions keep their existing signatures, so no `git grep` rewrites at call sites. The internal `_clients` dict moves to `event_bus.py` — any code importing `active_connections` directly (legacy set still maintained for backward compat per the existing comment) keeps working via re-export.

### Operator-facing behavior change: HPA default bump

The `autoscaling.maxReplicas` default moves from `1` to `3` in this PR. Operators who explicitly set `autoscaling.maxReplicas: 1` in their values are unaffected. Operators relying on the chart default (silently capped at `1` in v1.0) will see the pod scale up to 3 under load after upgrading — note in `CHANGELOG.md` under "Behavior changes" + flagged in the release-notes section that calls out v1.1 multi-replica readiness.

## Out of scope

- **Terminal stream multi-replica routing** — documented as an ingress concern. Operators add sticky-cookie annotation to `/ws/terminal/*`. Real cross-replica terminal routing (session table + smart ingress) is a v1.2 item if it becomes a real need.
- **Custom HPA metrics** (per-WS-connection, queue depth) — keep CPU. Not requested in #40.
- **Redis Streams / replay** — pub/sub at-most-once is sufficient for transient WS events.
- **Redis auth other than `REDIS_URL` password** — operators wanting mTLS or AUTH ACLs configure those via `REDIS_URL` query params (redis-py supports them); chart doesn't add a separate auth block.
- **Per-tenant channel isolation** — single global channel for v1.1. Per-tenant channels are coupled to Epic #36 Tenant Isolation Mode and ship with that work.

## Acceptance criteria (PR-close gate)

- [ ] `event_bus.py` shipped with the surface above; events.py public functions unchanged in signature
- [ ] `publish_event` calls `deliver_local` immediately + publishes to Redis when configured
- [ ] Subscriber echo-suppression by `source_replica_id` verified by unit test
- [ ] Helm chart `redis:` block + validator + secret-ref injection + helm tests green
- [ ] Integration test passes against a real Redis (CI service container)
- [ ] HPA defaults bumped from `1/1` to `1/3`; v1.0 pin comment removed
- [ ] Concept doc `docs/docs/concepts/multi-replica.md` covers operator setup (REDIS_URL, sticky-cookie ingress for terminals)
- [ ] Full pytest suite remains 0-fail
- [ ] All 65 existing call sites unchanged — verifiable with `git diff origin/dev -- '**/*.py' | grep -E '^[+-].*(broadcast_event|send_to_user|send_to_org)\('` returning **only** the lines in `apps/web-server/server/websockets/events.py` itself

## Estimate

~1 week. One PR if reviewable; can split into (a) bus + events.py refactor + tests and (b) helm chart + docs if it gets too big.

## Related

- Parent Epic [#35](https://github.com/olafkfreund/AIFactory/issues/35) — Enterprise v1.1
- Parent issue [#40](https://github.com/olafkfreund/AIFactory/issues/40) — original two-half issue
- Sibling spec — S3-compatible workspace storage (the other half of #40, to be written)
- Cross-ref Epic [#36](https://github.com/olafkfreund/AIFactory/issues/36) — Tenant Isolation Mode (per-tenant channel isolation is its concern, not this one)
