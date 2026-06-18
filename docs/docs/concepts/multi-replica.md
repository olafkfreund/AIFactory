---
title: Multi-replica deployment
sidebar_position: 10
---

# Multi-replica deployment

> *Scale AIFactory's web pod beyond one replica without losing real-time WebSocket events. Opt-in via Redis pub/sub.*

Single-replica AIFactory is the default — laptop installs, dev environments, and small pilots run one pod and everything Just Works. When you need to scale (more concurrent users, more in-flight tasks, HA across nodes), the v1.0 chart had a hard `replicaCount: 1` pin: starting a second pod would make WebSocket events fire on one pod and disappear from the others.

v1.1 closes that gap with an opt-in Redis pub/sub bridge that fan-outs events across all replicas. With Redis on, you can scale to `replicaCount: N` (or enable the HPA up to `maxReplicas: N`) and every replica's WebSocket clients receive every event — regardless of which replica fired it.

## When you need this

You want multi-replica when **any** of these apply:

- More than ~50 concurrent users on the portal (single replica starts to feel sluggish under WebSocket fan-out load).
- High availability across multiple K8s nodes (single replica = single point of failure for the control plane).
- Distinct internal traffic tiers — e.g. one replica serves long-running agent tasks, another serves interactive UI.

You **don't** need it for:

- Laptop installs.
- Single-developer / single-team pilots.
- Anything where the workload is bounded by agent throughput rather than control-plane throughput.

## What's in scope (and what's not)

| Aspect | v1.1 status |
|---|---|
| `broadcast_event`, `send_to_user`, `send_to_org` cross-replica | Works — fan-out via Redis |
| Task progress / log / status events | Works — they use the functions above |
| Agent-spawned events from any replica | Works — see above |
| Terminal WS streams (`/ws/terminal/*`) | **Stateful per-replica** — needs ingress sticky-cookie (see below) |
| Workspace storage across replicas | Separate spec (Epic #35 #40 S3 half) |

## Enable in AIFactory

### 1. Provision a Redis

Single instance is fine for the V1.1 pilot scale. Sentinel or Cluster mode work too via the `REDIS_URL` query syntax that `redis-py` supports.

If you want one inside the cluster, a minimal `bitnami/redis` chart or even a single-pod Deployment is enough. Production deployments typically point at managed Redis (ElastiCache, Azure Cache for Redis, Memorystore, etc.).

### 2. Create the Secret (production path)

```bash
kubectl create secret generic aifactory-redis \
  --from-literal=REDIS_URL='redis://:<password>@redis.aifactory.svc:6379/0' \
  --namespace aifactory
```

The Secret MUST have a key named `REDIS_URL`.

### 3. Flip the chart toggle + scale up

```yaml
# values.yaml overrides
replicaCount: 3                       # or enable HPA — both work

redis:
  enabled: true
  externalSecretName: aifactory-redis  # references the Secret above
  # channel: aifactory:events          # default; override only for shared Redis

# Recommended for any replicas>1 setup — stateful terminal streams
# need to pin to the replica that owns their rmux subprocess.
ingress:
  annotations:
    # Example for nginx-ingress; equivalent annotations exist for
    # most controllers. See your controller's WebSocket affinity docs.
    nginx.ingress.kubernetes.io/affinity: "cookie"
    nginx.ingress.kubernetes.io/affinity-mode: "persistent"
    nginx.ingress.kubernetes.io/session-cookie-name: "aifactory-affinity"
    nginx.ingress.kubernetes.io/session-cookie-path: "/ws/terminal"
```

`helm upgrade` and the next pod rollout will land with `REDIS_URL` + `REDIS_CHANNEL` injected. Each replica's startup logs a line like:

```
Redis pub/sub enabled — replica f0e9d8c7-… on channel 'aifactory:events'
```

The UUID is unique per pod instance; logging it makes Redis traffic correlatable when you're debugging cross-replica behavior.

## Dev-only inline URL

For local testing against a Docker Redis you can skip the Secret and inline the URL:

```yaml
redis:
  enabled: true
  url: "redis://localhost:6379/0"     # NEVER use in production
```

The chart's render-time validator blocks `helm template` when `redis.enabled=true` but both `url` and `externalSecretName` are empty.

## Failure modes

### Redis becomes unreachable mid-session

The web-server keeps accepting traffic. The subscriber task logs a WARNING and reconnects with exponential backoff (1s → 30s, capped). While Redis is down:

- Own-replica delivery still works — events fired by replica A still reach replica A's own clients (local delivery is synchronous and independent of the Redis path).
- Other replicas' clients miss events fired during the outage window — at-most-once semantics, no replay.

When Redis recovers, the subscriber reconnects and normal fan-out resumes. No app restart needed.

### Redis unreachable at startup

The pod starts anyway. Subscriber enters the reconnect loop. An ERROR log fires on the first failed connect; subsequent retries log at DEBUG to avoid log flooding.

### Multi-replica without Redis

The chart doesn't block this — single-replica deployments often want to test scaling locally first. But every replica's clients will only see events fired on their own replica. You'll see a startup log:

```
Redis pub/sub disabled (REDIS_URL unset) — in-process broadcasts only
```

Use this as a smoke check during deployment: if you see this log with `replicaCount > 1`, you forgot to enable Redis.

## What about terminal streams?

Terminal WebSocket streams (`/ws/terminal/{id}`) carry stateful rmux subprocesses tied to one specific replica. Cross-replica fan-out doesn't help — the subprocess's state can't be replicated cheaply.

The ingress sticky-cookie annotation in the example above is the recommended pattern: each browser session pins to one replica for the duration of its terminal interactions. The cookie path scoped to `/ws/terminal` means general-events WebSocket traffic on `/ws/events` is still free to land on any replica.

If you need true cross-replica terminal routing (e.g. one user's terminal needs to survive a pod restart on a different replica), that's tracked as v1.2 work — not in scope for v1.1.

## Operator notes

- **Channel name overrides**: if you run multiple AIFactory deployments against a single shared Redis, override `redis.channel` per deployment to avoid cross-talk.
- **Password rotation**: Update the Secret + restart pods. The subscriber connection is long-lived; rotating without restarting will fail authentication on the next reconnect attempt.
- **Multiple AIFactory clusters → one Redis**: works, but watch the channel namespacing carefully — events labeled `broadcast` go to every subscriber regardless of cluster.
- **Audit log**: cross-replica fan-out does NOT bypass `AuditLog` writes — those happen at the routes that emit events, before the bus is touched.

## Related

- Epic [#35](https://github.com/olafkfreund/AIFactory/issues/35) — Enterprise v1.1
- Issue [#40](https://github.com/olafkfreund/AIFactory/issues/40) — original two-half issue (Redis + S3)
- Design doc — `docs/plans/2026-05-28-redis-ws-fanout-design.md`
- [gVisor sandboxing](./gvisor-sandbox) — companion v1.1 isolation feature
