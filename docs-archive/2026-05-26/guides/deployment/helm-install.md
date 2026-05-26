# Helm installation runbook

> Audience: platform / SRE teams deploying AIFactory to Kubernetes.
> Compliance frameworks this supports: SOC2 CC6/CC7, NIST 800-53
> SC-7 (boundary protection), CIS Kubernetes Benchmark.
>
> Goal: install AIFactory on any conformant Kubernetes cluster
> (1.27+) with production-grade defaults — PSS-restricted, NetworkPolicy-
> enforced, ExternalSecrets-integrated, OIDC SSO-capable.

## What ships in the chart

| Resource | Purpose |
| --- | --- |
| `Deployment` | The AIFactory app pod (one replica in v1.0). |
| `Service` | ClusterIP fronting the app. |
| `ConfigMap` | Non-secret runtime config. |
| `ServiceAccount` | Dedicated SA, `automountServiceAccountToken=false`. |
| `NetworkPolicy` | Default-deny + 443 egress allowlist + DNS. |
| `PodDisruptionBudget` | `minAvailable: 1`. |
| `Ingress` | Opt-in (`.Values.ingress.enabled`). |
| `HorizontalPodAutoscaler` | Opt-in (`.Values.autoscaling.enabled`). |
| `ExternalSecret` | Opt-in (`.Values.externalSecrets.enabled`). |
| `StatefulSet + Secret + Service` (Postgres) | Opt-in (`.Values.postgres.bundled=true`). |

## Requirements

- **Kubernetes 1.27+**
- **Helm 3.16+**
- **Pod Security admission** with `restricted` policy enabled
  (recommended; the chart's defaults are designed for this).
- **External Secrets Operator** installed cluster-wide when
  `externalSecrets.enabled=true`.
- **An external Postgres** in production (RDS / Cloud SQL / Azure
  Postgres / on-prem). The bundled-Postgres mode is POC-only.

## Quick start (POC mode — bundled Postgres)

This produces a runnable but NOT production-grade install. Use it
to verify the chart on a kind / minikube cluster.

```bash
# 1. Add and update dependencies (currently none, but operators
#    who add CNPG via override should run this).
helm dep update charts/aifactory

# 2. Install with bundled Postgres + autoApply migrations.
helm install aifactory ./charts/aifactory \
  --set postgres.bundled=true \
  --set migrations.autoApply=true \
  --set image.repository=ghcr.io/dataseeek/aifactory \
  --set image.tag=1.0.0 \
  --wait --timeout=5m

# 3. Verify.
kubectl get pods -l app.kubernetes.io/name=aifactory
kubectl port-forward svc/aifactory 8080:80
curl http://localhost:8080/api/health
# {"status":"healthy","version":"1.0.0"}
```

## Production install

Production deployments use **external Postgres + ExternalSecrets +
OIDC + a non-Fernet KMS backend**.

### 1. Prerequisites

```bash
# External Secrets Operator (one-time cluster bootstrap)
helm repo add external-secrets https://charts.external-secrets.io
helm install eso external-secrets/external-secrets -n external-secrets --create-namespace

# Create a ClusterSecretStore pointing at your backend (Vault example;
# the AWS SM / Azure KV / GCP SM equivalents follow the ESO docs).
kubectl apply -f - <<EOF
apiVersion: external-secrets.io/v1beta1
kind: ClusterSecretStore
metadata:
  name: aifactory-secrets
spec:
  provider:
    vault:
      server: "https://vault.internal:8200"
      path: "secret"
      version: "v2"
      auth:
        kubernetes:
          mountPath: "kubernetes"
          role: "aifactory"
          serviceAccountRef:
            name: external-secrets
            namespace: external-secrets
EOF
```

### 2. Seed secrets in your backend

The chart consumes these keys per backend; pre-populate them.

| K8s Secret key (target) | Vault path example | AWS SM key example |
| --- | --- | --- |
| `database-url` | `secret/data/aifactory/db#url` | `aifactory/db:url` |
| `client-secret` (OIDC) | `secret/data/aifactory/oidc#client_secret` | `aifactory/oidc:client_secret` |
| `fernet-key` (only when `kms.backend=fernet`) | `secret/data/aifactory/kms#fernet_key` | `aifactory/kms:fernet_key` |

When `kms.backend` is one of `aws_kms` / `azure_kv` / `gcp_kms` /
`vault_transit`, the application authenticates to that KMS via
workload identity / IRSA / managed identity / Vault kubernetes auth;
no key material crosses the K8s Secret boundary.

### 3. Override values.yaml

```yaml
# values-prod.yaml
image:
  repository: registry.internal/aifactory
  tag: "1.0.0"

postgres:
  bundled: false  # external; DATABASE_URL via ExternalSecret
  externalSecretName: aifactory-db  # name of the K8s Secret to read

externalSecrets:
  enabled: true
  backend: vault                       # or aws-sm / azure-kv / gcp-sm
  clusterSecretStoreName: aifactory-secrets
  refs:
    databaseUrl: "secret/data/aifactory/db#url"
    oidcClientSecret: "secret/data/aifactory/oidc#client_secret"
    kmsFernetKey: "secret/data/aifactory/kms#fernet_key"

oidc:
  enabled: true
  provider: keycloak                   # or okta / azure_ad
  issuerUrl: https://keycloak.internal/realms/aifactory
  clientId: aifactory-web
  groupToRole:
    aifactory-admin: admin
    aifactory-member: member
  defaultRole: member

kms:
  backend: aws_kms                     # use workload identity, not in-K8s key
  awsKmsKeyId: arn:aws:kms:eu-west-1:1234:key/abcd-1234

migrations:
  autoApply: false                     # run Alembic as a separate Job

ingress:
  enabled: true
  className: nginx
  hosts:
    - host: aifactory.example.com
      paths:
        - path: /
          pathType: Prefix
  tls:
    - secretName: aifactory-tls
      hosts: [aifactory.example.com]

global:
  customCABundle:
    secretName: corp-root-ca  # when egress traverses a TLS-intercepting proxy
    key: ca-bundle.crt
```

```bash
helm install aifactory ./charts/aifactory -f values-prod.yaml \
  --wait --timeout=10m
```

### 4. Run the Alembic migration Job (production mode)

With `migrations.autoApply=false`, the app fails-fast on boot if the
schema isn't current. Run the migration out-of-band:

```bash
# v1.0 ships migration-as-Job as a documented manual procedure;
# v1.0.1 will template it as a Job resource in the chart.
kubectl run aifactory-migrate \
  --image=registry.internal/aifactory:1.0.0 \
  --restart=Never \
  --env "DATABASE_URL=$(kubectl get secret aifactory-db -o jsonpath='{.data.database-url}' | base64 -d)" \
  --env "KMS_FERNET_KEY=$(kubectl get secret aifactory-kms -o jsonpath='{.data.fernet-key}' | base64 -d 2>/dev/null || echo "")" \
  -- python -m alembic upgrade head

kubectl logs aifactory-migrate
kubectl delete pod aifactory-migrate
```

## Verifying the install

### Helm side

```bash
helm list                          # status=deployed
helm get values aifactory          # confirm overrides applied
helm get manifest aifactory | grep -E "kind:" | sort -u
```

### Kubernetes side

```bash
kubectl get pods -l app.kubernetes.io/name=aifactory
kubectl describe deployment aifactory | grep -A 10 "Containers:"
kubectl get networkpolicy aifactory -o yaml
```

### Application side

```bash
# /api/health from outside (assumes ingress is wired):
curl -sf https://aifactory.example.com/api/health

# OIDC login flow (browser):
# Open https://aifactory.example.com → click "Sign in with SSO"
# → redirected to IdP → land back on the app, signed in.
```

## Upgrade

Standard Helm upgrade. The chart's `RollingUpdate` strategy is set
to `maxSurge: 0` so v1.0's single replica is replaced atomically
(no double-write window).

```bash
helm upgrade aifactory ./charts/aifactory -f values-prod.yaml --wait

# If you changed schema (post-P3 → P4 upgrade), run the migration
# Job BEFORE the upgrade so the new pods boot against a current DB.
```

## Rollback

```bash
helm rollback aifactory 0   # previous release
helm rollback aifactory     # one step back
```

> **WARNING:** Rolling back through a `forward-only` migration
> (e.g. P2.3 c6e3b2d4a8f0_encrypt_credentials) requires restoring
> the database from a backup taken before the migration ran. See
> [encrypted-secrets-dr.md](../operations/encrypted-secrets-dr.md).

## Troubleshooting

| Symptom | Cause | Resolution |
| --- | --- | --- |
| Pod stays in `Init:0/1` forever | PSS-restricted admission rejected the manifests | `kubectl describe pod` — look for `violates PodSecurity "restricted"`; check that your namespace label matches `pod-security.kubernetes.io/enforce=restricted`. |
| App pod CrashLoopBackOff with `KMS_FERNET_KEY env var is not set` | Fernet backend selected but no Secret seeded | `kubectl get secret aifactory-kms` — if missing, your ExternalSecret hasn't reconciled. `kubectl describe externalsecret aifactory-kms`. |
| OIDC login button returns 404 | OIDC not configured | Set `oidc.enabled=true` + `issuerUrl` + `clientId` + secret. |
| `helm install` hangs | NetworkPolicy blocks readiness probes | Verify your CNI supports NetworkPolicy (Calico / Cilium / kube-router). Without CNI support, the policy is enforced "best effort" — but probes targeting the pod from the kubelet should still work. |
| TLS errors talking to IdP / KMS | TLS-intercepting proxy not trusted by pod | Set `global.customCABundle.secretName` to the K8s Secret holding your corporate CA. |
| `helm lint` warns about missing CNPG dep | The chart no longer depends on CNPG; check you're on v1.0.0+ |  |

## Related

- [oidc-setup.md](../operations/oidc-setup.md) — OIDC SSO setup.
- [kms-rotation-runbook.md](../operations/kms-rotation-runbook.md) — KMS root rotation.
- [encrypted-secrets-dr.md](../operations/encrypted-secrets-dr.md) — DR after secrets-layer incidents.
- `charts/aifactory/values.yaml` — full value reference.
- Source: `charts/aifactory/templates/`.
