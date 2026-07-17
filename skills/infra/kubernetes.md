# kubernetes

> Source: curated best practices | 2026

---

# Kubernetes - Production workloads: probes, resources, security context

A production Kubernetes 1.30 workload declares its health (liveness/readiness/startup probes), its resource envelope (requests and limits), and its security posture (non-root, read-only root filesystem, dropped capabilities). Getting these right is the difference between a Deployment that self-heals and rolls out cleanly and one that thrashes, gets OOM-killed, or runs privileged. This skill covers Deployments, Services, probes, resource management, PodSecurity, and safe rollouts.

## When to Activate

Use when the task involves Kubernetes:
- Writing Deployment/Service/StatefulSet/Ingress manifests
- Configuring probes, resources, or autoscaling (HPA)
- Pod security context, RBAC, or PodSecurity standards
- Rollouts, PodDisruptionBudgets, ConfigMaps/Secrets
- Debugging CrashLoopBackOff / OOMKilled / pending pods

## Patterns and Best Practices

### Production Deployment — probes, resources, security context

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: api
spec:
  replicas: 3
  selector: { matchLabels: { app: api } }
  strategy:
    type: RollingUpdate
    rollingUpdate: { maxSurge: 1, maxUnavailable: 0 }   # never drop below capacity
  template:
    metadata:
      labels: { app: api }
    spec:
      securityContext:               # pod-level
        runAsNonRoot: true
        runAsUser: 10001
        seccompProfile: { type: RuntimeDefault }
      containers:
        - name: api
          image: registry/api@sha256:<digest>     # pin by digest, not a mutable tag
          ports: [{ containerPort: 8080 }]
          resources:
            requests: { cpu: "100m", memory: "128Mi" }   # scheduler guarantee
            limits:   { cpu: "500m", memory: "256Mi" }   # hard cap (memory limit = OOM ceiling)
          securityContext:           # container-level
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities: { drop: ["ALL"] }
          startupProbe:              # slow starts: gate liveness until app is up
            httpGet: { path: /health, port: 8080 }
            failureThreshold: 30
            periodSeconds: 2
          livenessProbe:             # restart if wedged
            httpGet: { path: /health, port: 8080 }
            periodSeconds: 10
          readinessProbe:            # remove from Service endpoints if not ready
            httpGet: { path: /ready, port: 8080 }
            periodSeconds: 5
          volumeMounts:
            - { name: tmp, mountPath: /tmp }   # read-only root needs writable tmp
      volumes:
        - name: tmp
          emptyDir: {}
```

Key rules:
- **Requests** are what the scheduler reserves; **limits** are hard caps. Set memory `request == limit` for predictable OOM behavior; leave CPU limit generous or unset to avoid throttling.
- **Readiness** gates traffic; **liveness** restarts. Point them at *different* endpoints — a liveness probe that checks a downstream dependency causes cascading restarts.
- **Startup probe** protects slow-booting apps so liveness doesn't kill them mid-boot.
- `maxUnavailable: 0` keeps full capacity during rollouts.

### Service + PodDisruptionBudget

```yaml
apiVersion: v1
kind: Service
metadata: { name: api }
spec:
  selector: { app: api }
  ports: [{ port: 80, targetPort: 8080 }]
---
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata: { name: api }
spec:
  minAvailable: 2                    # voluntary disruptions (node drain) keep 2 up
  selector: { matchLabels: { app: api } }
```

### Config and secrets

Mount config via ConfigMap and secrets via `Secret` (as files, not env, to avoid leaking in `/proc`). Reference external secret managers (External Secrets Operator / CSI driver) rather than committing base64 to git — base64 is encoding, not encryption.

### Autoscaling

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata: { name: api }
spec:
  scaleTargetRef: { apiVersion: apps/v1, kind: Deployment, name: api }
  minReplicas: 3
  maxReplicas: 10
  metrics:
    - type: Resource
      resource: { name: cpu, target: { type: Utilization, averageUtilization: 70 } }
```

HPA needs resource **requests** set to compute utilization — without requests, CPU autoscaling can't work.

### Debugging quick reference

- `CrashLoopBackOff` → `kubectl logs --previous`; bad command, missing config, failing liveness.
- `OOMKilled` → memory limit too low; raise limit or fix leak.
- `Pending` → unschedulable: insufficient resources, unbound PVC, or node selector/taint mismatch.
- `ImagePullBackOff` → wrong image ref or missing registry pull secret.

## Anti-patterns

- No resource requests/limits — noisy-neighbor and unschedulable-or-OOM chaos.
- Same endpoint for liveness and readiness, or liveness checking downstreams — cascading restarts.
- Running as root / no security context — privileged blast radius.
- Mutable image tags (`:latest`) in Deployments — non-reproducible, surprise rollouts; pin by digest.
- CPU **limits** set tight — needless throttling; prefer requests + generous/absent CPU limit.
- Secrets as plain env vars committed as base64 in git.
- `replicas: 1` for a stateless service with no PDB — no availability during node drains.
- Skipping `readinessProbe` — traffic sent to pods that aren't ready during rollout.
