# AIFactory Helm chart

Production Helm chart for self-hosted enterprise deployments of
AIFactory (Epic #26). PSS-restricted by default; NetworkPolicy-
enforced egress; integrates with the four major external-secret
backends (Vault, AWS Secrets Manager, Azure Key Vault, GCP Secrets
Manager).

## Quick start (POC mode — bundled Postgres)

```bash
helm dep update
helm install aifactory ./charts/aifactory \
  --set postgres.bundled=true \
  --set image.repository=ghcr.io/olafkfreund/aifactory \
  --set image.tag=1.0.0
```

## Production install (external Postgres + ExternalSecrets + OIDC)

See [guides/deployment/helm-install.md](../../guides/deployment/helm-install.md)
for the full operator runbook — per-cloud setup, secret seeding,
migration job mode, customCABundle for TLS-intercepting proxies.

## Values surface

`values.yaml` is the primary config surface. Schema-validated via
`values.schema.json` (so `helm lint --strict` catches typos).

| Section | Purpose |
| --- | --- |
| `image` | Container image reference (override repo for mirror registries). |
| `replicaCount` | Pinned to 1 for v1.0 (WebSocket fan-out limitation). |
| `resources` | CPU/memory requests + limits. |
| `podSecurityContext` / `containerSecurityContext` | PSS-restricted defaults. |
| `service` / `ingress` | Network exposure. |
| `serviceAccount` / `rbac` | Pod identity, and the namespace-scoped Role the RFC-0016 build lane needs (`rbac.jobSandbox.enabled`, on by default — see below). |
| `networkPolicy` | Default-deny + 443 egress allowlist. |
| `migrations` | Alembic Job mode (autoApply=false in prod). |
| `postgres` | External (default) or bundled CNPG sub-chart. |
| `externalSecrets` | One of: vault / aws-sm / azure-kv / gcp-sm. |
| `oidc` | OIDC SSO settings (Epic #26 P3). |
| `kms` | At-rest encryption backend (Epic #26 P2). |
| `global.customCABundle` | TLS-intercepting proxy support. |

## RBAC for the build lane (`rbac.jobSandbox`)

**User story.** As someone self-hosting AIFactory, I want `helm install` to give
me a control plane that can actually run a build, rather than one that reaches
the API server and is refused on every call.

AIFactory does not run builds inside its own pod. Under RFC-0016 it creates a
Kubernetes **Job per task** and streams that Job's pod logs back
(`core/kube_sandbox.py`, `services/build_backend.py`,
`services/build_log_stream.py`). That needs a Role bound to the pod's
ServiceAccount, in the release namespace.

| Value | Default | Behaviour |
| --- | --- | --- |
| `rbac.jobSandbox.enabled` | `true` | Renders a namespace-scoped `Role` + `RoleBinding` granting `jobs` (create/get/list/watch/delete), `pods` (get/list/watch) and `pods/log` (get) to the chart's ServiceAccount. Nothing cluster-wide; no `secrets`. |
| `rbac.jobSandbox.enabled` | `false` | No Role is rendered. The control plane cannot create a build Job — use this only for an install that will never build (e.g. a read-only portal), and pair it with `serviceAccount.automountServiceAccountToken: false`. |
| `rbac.create` | `false` | **Inert.** No template references it. It predates the build lane and is kept only so an existing values file does not break. Setting it to `true` renders nothing — `rbac.jobSandbox.enabled` is the switch you want. |

This defaults ON, unlike TFactory's equivalent, because AIFactory has no
non-Kubernetes build lane to fall back to: an install with it off cannot run a
build at all.

## Requirements

- Kubernetes 1.27+
- Helm 3.16+
- (optional) `cloudnative-pg` chart repo (when `postgres.bundled=true`)
- (optional) External Secrets Operator installed cluster-wide
  (when `externalSecrets.enabled=true`)

## License

Dual-licensed: MIT OR GPL-3.0 — see [LICENSE](../../LICENSE).
