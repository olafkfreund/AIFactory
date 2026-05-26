# AIFactory Enterprise v1.0 — Design Spec

> Created: 2026-05-24
> Status: Approved (pending Epic creation)
> Authors: olafkfreund
> Approval gate: super-brainstorm interview, 13 decisions locked

## 1. Summary

Package AIFactory for self-hosted deployment in fintech and bank customer environments. v1.0 ships a credible pilot-MVP — Helm chart, Postgres backend, OIDC SSO, encrypted secrets at rest, hardened container, SOC2/GDPR evidence baseline — within an 8-week, 1-engineer budget. v1.1 ships the full enterprise-credible story (Tenant Isolation Mode, gVisor sandboxing, LiteLLM gateway, S3 workspaces, SAML/SCIM, OpenTelemetry, ISO 27001 evidence formalization). Air-gap, OCP-native support, Kata sandboxing, Azure OpenAI, and FedRAMP are explicitly out of v1 scope and tracked in v1.x.

The current product has a multi-tenancy-ready data model (`User`, `Organization`, `OrganizationMember.role`, `AuditLog`) but a single-tenant LAN-macvlan Dockerfile, plaintext SQLite secrets, no SSO, no K8s/Helm, and no Prometheus/OTel. This spec describes the v1.0 work that closes the gap from "works on my LAN" to "deployable in a bank's K8s cluster behind their IdP and observability stack."

## 2. Locked decisions

| # | Decision | Value |
|---|---|---|
| 1 | GTM topology | Self-hosted only |
| 2 | K8s targets v1.0 | Vanilla + EKS / AKS / GKE, PSS=restricted; OpenShift overlay = v1.1+ |
| 3 | Egress posture | Egress-whitelist baseline (assume customer egress proxy); air-gap deferred |
| 4 | Compliance evidence v1.0 | SOC2 Type II + GDPR; ISO 27001 evidence formalization = v1.1 |
| 5 | Licensing | Pure AGPL-3.0 for v1; commercial/dual/open-core deferred until first paying deal |
| 6 | Intra-install tenancy | v1.0 = logical separation (row-level + RBAC + audit); Tenant Isolation Mode (per-tenant namespace) = v1.1 |
| 7 | Sandbox runtime | v1.0 = PSS-restricted pod default; gVisor RuntimeClass opt-in = v1.1; Kata/Firecracker = v1.x |
| 8 | LLM stack | v1.0 = existing provider factory direct to Anthropic; LiteLLM gateway + Bedrock + Vertex = v1.1; Azure OpenAI = v1.x |
| 9 | Timeline / team | 1 engineer, ~8 weeks to v1.0 pilot-MVP |
| 10 | SSO v1.0 | OIDC-only with JIT user provisioning; SAML 2.0 + SCIM 2.0 = v1.1 |
| 11 | Postgres v1.0 | BYOPostgres (`DATABASE_URL` from ExternalSecret) for production; CloudNativePG as optional Helm dependency for POC |
| 12 | Secrets v1.0 | ExternalSecrets Operator in Helm chart + in-app SQLAlchemy `EncryptedString` (KMS envelope crypto) for credential columns |

## 3. v1.0 Architecture

```
                 ┌── Ingress (NGINX / Envoy) — TLS + WAF ──┐
                 │                                          │
              ┌──┴── OIDC redirect ────────────────────────┴── Customer IdP
              │                                                 (Keycloak / Okta /
              ▼                                                  Azure AD / Auth0)
        ┌──────────────────────────┐
        │ aifactory-web Deployment │   replicas: 1–N (HPA on CPU/RPS)
        │ FastAPI + JWT + audit    │   PSS=restricted; non-root; RO root fs
        └─────────────┬─────────────┘
                      │
       ┌──────────────┼──────────────┬──────────────────────┐
       ▼              ▼              ▼                      ▼
 ┌────────────┐  ┌──────────┐  ┌────────────┐    ┌────────────────┐
 │ Postgres   │  │ PVC      │  │ Anthropic  │    │ Prometheus     │
 │ BYO (RDS / │  │ workspace│  │ API        │    │ scrapes        │
 │  Aurora /  │  │ (RWX)    │  │ via egress │    │ /metrics       │
 │  Crunchy / │  │          │  │ proxy      │    │                │
 │  CNPG)     │  │          │  │            │    │ structlog →    │
 │ +column    │  │          │  │            │    │ stdout         │
 │  KMS-env   │  │          │  │            │    │ (Loki / ELK)   │
 │  encrypt   │  │          │  │            │    │                │
 └────────────┘  └──────────┘  └────────────┘    └────────────────┘
       ▲              ▲
       │              │
 ┌─────┴──────────────┴────────────────────────────────────────────┐
 │ ExternalSecrets Operator → Vault / AWS SM / Azure KV / GCP KMS │
 └────────────────────────────────────────────────────────────────┘
```

### 3.1 Components

#### 3.1.1 Container image

- **Base**: `cgr.dev/chainguard/python:latest-dev` for build, `cgr.dev/chainguard/python:latest` for runtime. Distroless, glibc-based, FIPS-friendly successor candidate. **Production builds pin to a digest** (`@sha256:...`), captured in CI and updated by Renovate; `:latest` is documentation-only.
- **User**: non-root (`uid=65532`); no `useradd` shenanigans, no `gosu`, no `iptables` in entrypoint.
- **Capabilities**: dropped, `securityContext.allowPrivilegeEscalation=false`, `readOnlyRootFilesystem=true` (mount writable `/tmp` and `/var/cache` as `emptyDir`).
- **Multi-arch**: `linux/amd64` + `linux/arm64` via `docker buildx`.
- **Supply chain**: Syft SBOM (`spdx-json` + `cyclonedx-json`) attached to image; Trivy scan in CI with fail-on-`HIGH`/`CRITICAL`; cosign keyless signing (Sigstore) via GitHub OIDC.
- **Frontend**: built in a Node 24 stage, output static assets copied into runtime stage at `/app/static/`. FastAPI serves them via `StaticFiles`.
- **Entrypoint**: pure-Python; no shell escape, no network-policy mutation. The current `docker-entrypoint.sh` LAN-firewall logic is removed — egress control moves to the customer's `NetworkPolicy`.

#### 3.1.2 Database

- Production: customer-provided Postgres ≥ 15 reachable via `DATABASE_URL` injected from `ExternalSecret`.
- Driver: `asyncpg` via SQLAlchemy 2.x async engine.
- Migrations: Alembic. **Two modes:** `migrations.autoApply=true` (default; idempotent first-boot apply, suitable for cloud-native banks) or `migrations.autoApply=false` (chart emits an Alembic-only Helm `Job` and the DBA runs it out-of-band; required for banks where the app role lacks DDL privileges).
- **Required Postgres privileges** on the schema the app owns: `CONNECT`, `USAGE`, `CREATE`, `SELECT`/`INSERT`/`UPDATE`/`DELETE` on all tables, `USAGE`/`SELECT` on sequences. **No superuser, no `CREATE EXTENSION`.** Schema must be pre-created by the customer's DBA when `migrations.autoApply=false`.
- **No Postgres extensions required.** UUIDs are generated app-side (`uuid.uuid4()`); no `pgcrypto`/`uuid-ossp` dependency. Documented in chart README.
- SQLite path retained for local dev only, gated behind `APP_ENV=dev`.
- POC: optional Helm sub-chart dependency on `cloudnative-pg` operator (`postgres.bundled=true`), exposing a generated `Cluster` with one primary + one async replica.

#### 3.1.3 Secrets

Two distinct concerns, two distinct mechanisms:

**Distribution (how secrets reach the pod):** chart ships `ExternalSecret` templates for the four common backends (Vault, AWS SM, Azure KV, GCP KMS). Customer picks one in `values.yaml`. Required secrets: `database-url`, `jwt-secret`, `oidc-client-secret`, `kms-master-key-arn` (or equivalent), `anthropic-api-key`.

**Storage (how the DB stores credentials):** new SQLAlchemy `EncryptedString` `TypeDecorator` performing AES-256-GCM envelope encryption with **per-organization data keys** (not per-row, not global). Each Organization gets one wrapped data key generated on creation, stored in `kms_data_keys`. Ciphertext columns reference the org's `kms_data_key_id`. Blast radius on compromise = one organization. KMS calls happen on key creation/rotation, not on every row read: each unwrapped data key is **cached in-process for the pod's lifetime** in an LRU keyed by `org_id`, evicted on `kms_data_keys.rotated_at` change (detected via a polled flag on the row, every 60 s). Pod restart = one KMS unwrap per active org. Applied to all credential-bearing columns: `IntegrationToken.value`, `Settings.api_token`, OAuth refresh tokens, Anthropic key.

KMS abstraction layer supports four backends behind a single interface:
- `aws_kms` (boto3, `kms:Encrypt`/`kms:Decrypt`)
- `azure_kv` (azure-keyvault-keys, `wrapKey`/`unwrapKey`)
- `gcp_kms` (google-cloud-kms, `encrypt`/`decrypt`)
- `vault_transit` (hvac, `transit/encrypt/{key}`)
- `fernet` (local-key for POC/dev, never recommended for production)

Key rotation: documented runbook; root-key rotation triggers a re-wrap of the per-organization data keys via a background job (`server/jobs/rotate_kms_root.py`); plaintext is never re-encrypted.

#### 3.1.4 Authentication

- Existing JWT (python-jose) and legacy bearer auth retained as-is.
- New OIDC flow via `authlib`. Configurable IdP per install (`values.yaml: oidc.issuer`, `oidc.client_id`, `oidc.client_secret_ref`).
- **Authorization Code with PKCE is mandatory.** No implicit flow. `state` parameter validated server-side.
- Just-in-time user provisioning on first login: create `User` row from OIDC claims (`sub`, `email`, `name`), create `OrganizationMember` row with role from configurable claim-mapping (default `groups` claim → role map in `values.yaml`).
- Login flow: `/api/auth/oidc/login` → redirect to IdP (with PKCE challenge + state) → callback at `/api/auth/oidc/callback` (verifies state, PKCE) → mint internal JWT → set HTTP-only cookie + return access token to frontend.
- **Logout flow:** `POST /api/auth/oidc/logout` → delete the server-side **refresh-session** row (keyed by refresh-token JTI) → redirect to IdP `end_session_endpoint` (if advertised in discovery) for SSO-wide logout. **Note:** the 15-min access JWT is self-contained and remains valid until expiry — we deliberately do *not* maintain an access-token denylist (avoids the operational cost of a hot blacklist; the short TTL is the mitigation).
- **JWT TTL:** access token 15 min, refresh token 8 h. Refresh path calls IdP `userinfo` endpoint and aborts if the IdP reports the user disabled/deleted — effective session-revocation latency ≤15 min after IdP disablement. **`userinfo` result is cached for the refresh-token lifetime** (max 8 h) keyed by `sub` to avoid hammering the IdP on every refresh; cache is invalidated on logout.
- Back-channel logout (`backchannel_logout_uri`): **deferred to v1.1**. Documented as a known limitation; mitigation is the short access-JWT TTL above.
- Legacy bearer token preserved behind feature flag `LEGACY_BEARER_ENABLED` (default off for new installs; on for upgrade-in-place).
- Settings UI: read-only display of OIDC config (write-via-`values.yaml` only).

#### 3.1.5 Audit log

- Existing `AuditLog` model retained. Retention policy field added: `audit_logs.retention_until` (timestamp); rows past `retention_until` deleted by a daily background job. Default retention 13 months (SOC2 12-month requirement + 1-month buffer).
- New endpoint `GET /api/audit/export?format={json|csv}&from=…&to=…` — requires `audit:read` permission, streams response, includes hash-chain field per row for tamper evidence.
- Hash chain: each `AuditLog` row carries `prev_hash` (SHA-256 of the previous row's `(id, action, user_id, org_id, created_at, details_json)`); export includes both the row and the chain for offline verification.
- **Threat-model limitation of the v1.0 chain:** an attacker with full DB write access can rewrite the entire chain end-to-end. The chain proves *internal consistency*, not *untamperedness*. **Externally-signed anchors** (sign the latest `prev_hash` with the KMS root key into a separate `audit_anchors` table on a daily cron) are **deferred to v1.1**. This limitation is documented in the SOC2 evidence doc — bank auditors will ask, the answer is "anchor in v1.1; today the mitigation is restrictive DB write privileges".
- New endpoint `POST /api/users/{id}/gdpr-erasure` — hard-deletes user PII (`email`, `name`, OAuth tokens), preserves `AuditLog` rows but replaces `user_id` with the user's sha256 hash and clears `details_json` PII fields via configurable redaction rules.
- LLM-call audit (prompt/response capture) moves to **v1.1** as part of the LiteLLM gateway, which is where all model traffic will be centrally inspected. v1.0 audits the *agent-task* boundary (start/end/result), not individual model calls.

#### 3.1.6 Observability

- `structlog` configured for JSON output to stdout; correlation IDs propagated via `X-Request-ID` header (generated if absent) and threaded through `contextvars`.
- `prometheus-fastapi-instrumentator` exposes `/metrics` (default histograms + counters + per-route latency). **Cardinality is capped** by using FastAPI route *templates* (`/api/projects/{id}/tasks`) as the `handler` label rather than raw paths — `instrumentator.instrument(app, ...)` is configured with `should_group_status_codes=True` and `excluded_handlers` to skip noisy paths. Documented in chart README so customers sharing a Prometheus cluster don't hate us. Authenticated metrics path (`metrics.scrape_token_ref` in `values.yaml`) — Prometheus uses a bearer-token scrape config.
- ServiceMonitor template in chart for kube-prometheus-stack auto-discovery.
- Pre-built Grafana dashboard JSON shipped under `guides/observability/grafana-aifactory.json`: request rate, p50/p99 latency, error rate, agent-task throughput, audit-log write rate, OIDC-login success/failure.

OpenTelemetry distributed tracing: deferred to v1.1.

#### 3.1.7 Helm chart

Single chart `aifactory` under `charts/aifactory/`:

```
charts/aifactory/
  Chart.yaml
  Chart.lock
  values.yaml
  values.schema.json          # JSON Schema for `helm lint --strict`
  README.md
  templates/
    _helpers.tpl
    deployment.yaml
    service.yaml
    ingress.yaml
    hpa.yaml
    servicemonitor.yaml
    networkpolicy.yaml
    configmap.yaml
    externalsecret-database.yaml
    externalsecret-jwt.yaml
    externalsecret-oidc.yaml
    externalsecret-kms.yaml
    externalsecret-anthropic.yaml
    rbac.yaml
    poddisruptionbudget.yaml
    serviceaccount.yaml
  charts/
    cloudnative-pg/           # vendored as optional sub-chart
```

Default values: PSS=restricted security context, RWX PVC for workspaces (default StorageClass), HPA on 50% CPU + 20 RPS, single replica (HPA scales 1–5), no ingress class hardcoded. Configurable `priorityClassName`, `nodeSelector`, `tolerations`, `affinity` for banks with dedicated platform classes.

NetworkPolicy default-deny + explicit allow: `egress` to `0.0.0.0/0` on `:443` (Anthropic API + IdP + KMS APIs); `ingress` only from `ingress-controller` namespace.

### 3.2 Data flow

```
User → Browser → Ingress → web Pod
                            │
                            ├── DB read/write (asyncpg → Postgres)
                            ├── KMS unwrap (boto3 / azure / gcp / hvac)
                            ├── Workspace fs (PVC)
                            ├── LLM call (Anthropic API → egress proxy → internet)
                            └── Audit write (every privileged action)
```

WebSocket fan-out remains in-process (single web replica acceptable for v1.0 pilot); Redis pub/sub deferred to v1.1. HPA min/max set to 1/1 by default in v1.0; documented as a known limitation.

### 3.3 Migrations

The data model already has `User`, `Organization`, `OrganizationMember`, `AuditLog`. v1.0 adds:

- New columns: `audit_logs.retention_until TIMESTAMPTZ`, `audit_logs.prev_hash CHAR(64)`, `users.gdpr_erased_at TIMESTAMPTZ`.
- Column type changes: `integration_tokens.value`, `settings.api_token`, `oauth_tokens.refresh_token`, `users.anthropic_api_key` → `EncryptedString`. Migration includes a one-time backfill: read plaintext, encrypt, write back. Documented runbook for staging the rotation.
- New table `kms_data_keys (id UUID, org_id UUID FK, wrapped_key BYTEA, kms_key_id TEXT, created_at TIMESTAMPTZ, rotated_at TIMESTAMPTZ)` — one wrapped data key per Organization (see §3.1.3).

All migrations via Alembic. Idempotent on re-run. Roll-back tested in CI.

### 3.4 Failure & rollback

- **OIDC IdP outage**: legacy bearer token (if enabled) and existing JWT sessions continue working. New logins fail; user-facing error explains.
- **KMS outage**: app refuses to start (no decryption possible). PodCrashLoopBackoff → operator paged.
- **Postgres outage**: app refuses to start. Standard K8s readiness probe failure.
- **Failed deploy rollback**: standard Helm rollback (`helm rollback`) covers everything *except* the encrypted-column migration in P2. **That migration is one-way: there is no automated downgrade from `EncryptedString` columns back to plaintext.** Rolling back below the v1.0 baseline requires `pg_restore` from the pre-migration backup taken in the runbook. This is acceptable for a one-time v0 → v1.0 upgrade; documented prominently in the migration runbook.

## 4. v1.0 phase plan (8 weeks, 1 engineer)

The plan is intentionally **parallelised across phases that don't share dependencies** — P3 (OIDC) and P4 (Helm chart) overlap because they touch different files; P5 (audit) and P6 (observability) overlap because both are mostly additive middleware. This is a *plan*, not a wish: if either parallelisation slips, the pre-agreed scope cuts in §7 fire (drop Azure AD preset, drop CSV export, push upgrade-drill to v1.0.1).

| Week | Phase(s) | Deliverable | Verification |
|---|---|---|---|
| **1** | P0 — Container hygiene | Chainguard base (digest-pinned), non-root, no NET_ADMIN, multi-arch buildx, Trivy/Syft/cosign in CI | Trivy scan no HIGH/CRITICAL, signature verified, runs as uid 65532 |
| **2** | P1 — Postgres backend | asyncpg driver, Alembic migrations + optional Job-mode, CI tests with postgres-in-CI | Smoke test against Postgres 15 + 16; SQLite path still works in dev; install with `migrations.autoApply=false` |
| **3** | P2 — Encrypted secrets at rest | `EncryptedString` `TypeDecorator`, KMS envelope, per-org `kms_data_keys`, migrate columns, backfill runbook | Round-trip test for all 4 KMS backends + Fernet local; no plaintext credentials in `pg_dump`; key-rotation runbook walked through |
| **4** | P3 (start) — OIDC SSO | authlib client w/ PKCE, JIT provisioning, login flow against Keycloak | Keycloak Docker container in CI; login + JIT-provisioning e2e green |
| **5** | P3 (finish) ∥ P4 (start) | OIDC logout flow + JWT TTL + `userinfo` caching + Okta/AzureAD presets ∥ Helm chart skeleton (Deployment/Service/Ingress/HPA) | Full login/logout/token-refresh test; user-disabled-in-IdP test shows ≤15-min revocation; `helm install` succeeds on kind |
| **6** | P4 (finish) | All Helm templates, NetworkPolicy, ExternalSecret for 4 backends, CNPG optional dep, `customCABundle`, `priorityClassName`/`nodeSelector` knobs | `helm lint --strict`, `kubeconform`, deploy to kind cluster in CI, install on EKS+RDS once |
| **7** | P5 ∥ P6 | Audit retention + JSON/CSV export + GDPR erasure + hash chain ∥ structlog JSON + Prometheus instrumentator (cardinality-capped) + ServiceMonitor + Grafana dashboard | Export round-trip + hash-chain verification; erasure-then-audit-still-valid; scrape works; dashboard renders; logs parse with `jq` |
| **8** | P7 — Evidence + docs + ship-readiness drills | SOC2 control mapping, deployment runbook, threat-model, install/upgrade guide, image-mirroring procedure, **backup/restore drill**, **upgrade-in-place drill**, DPIA/data-flow diagram | Runbook walked by a non-author; backup/restore round-trip preserves encrypted-column decryption; `helm upgrade` from synthetic v0.x → v1.0 succeeds |

There is **no dedicated buffer week** — week 8 is fully allocated to P7. The plan absorbs slippage via the pre-agreed scope cuts in §7. If you reach the end of week 6 and P3/P4 are unfinished, you trigger Cut 1 (drop Azure AD preset) immediately and revisit at end of week 7.

LLM-call prompt/response audit (originally in P5) moves to v1.1, where the LiteLLM gateway is the natural inspection point. v1.0 audits only the agent-task boundary (start/end/result).

## 5. v1.1 roadmap (~6–10 weeks post-pilot)

Tracked as a separate Epic, blocked on v1.0 closeout:

- **Tenant Isolation Mode** — pod-spawner controller (creates per-tenant Namespace + RBAC + NetworkPolicy + ExternalSecret per Organization on creation); per-tenant S3 prefix + IAM policy; per-tenant Vault path
- **gVisor RuntimeClass opt-in** — `runtimeClassName: gvisor` toggle in `values.yaml`; CI smoke test with gVisor enabled
- **LiteLLM gateway** — in-cluster proxy in front of all LLM calls; per-tenant token-budget meter, rate limit, model allowlist, prompt+response audit
- **Bedrock + Vertex provider classes** — boto3 `bedrock-runtime` integration; `google-cloud-aiplatform` integration; per-tenant credentials via ExternalSecret
- **S3-compatible workspace storage** — `fsspec` abstraction; workspace lifecycle hooks (upload on completion, restore on resume); supports AWS S3, Azure Blob, GCS, MinIO
- **Redis pub/sub for WebSocket fanout** — `redis-py` async client; replace in-process emitter; enables `replicas > 1`
- **SAML 2.0 + SCIM 2.0** — `python-saml` integration; SCIM 2.0 endpoint for user/group provisioning
- **OpenTelemetry distributed tracing** — OTel SDK + OTLP exporter; auto-instrumentation for FastAPI + SQLAlchemy + asyncpg + httpx; sampler config in `values.yaml`
- **ISO 27001 evidence formalization** — Annex A control mapping doc; access-review export endpoint; data-classification metadata in `AuditLog`

## 6. v1.x roadmap (opportunistic, no committed timeline)

- OpenShift overlay: `Route` resources, SCC-compatible pod specs, OperatorHub bundle, CSV
- Kata Containers / Firecracker sandbox runtime classes
- Azure OpenAI provider (prompt-portability work + tool-use schema translation)
- Air-gap path: offline-install tarball, mirror-registry doc, on-prem LLM via vLLM/Ollama
- BYOC managed control plane (we run control, customer runs data plane)
- FedRAMP Moderate: FIPS 140-2/3 crypto modules, separate GovCloud images, 3PAO assessment

## 7. Risks & open questions

### Risks

| Risk | Mitigation |
|---|---|
| Pilot bank discovers they need Tenant Isolation Mode mid-pilot | Explicit upfront: "v1.0 = one logical tenant per install; multi-team isolation = v1.1." Document in onboarding |
| KMS integration takes longer than estimated due to customer variance | ExternalSecrets makes secret distribution uniform across 4 backends. In-app crypto uses a generic interface with Fernet (local key) as POC fallback |
| OIDC quirks per IdP (claim mappings differ between Okta / Azure AD / Keycloak) | Ship a configurable claim-mapping in `values.yaml` and tested presets for the top 3 IdPs |
| Single-replica web pod becomes a bottleneck for the pilot | Document the limitation; offer a paid v1.1 fast-track if pilot scales beyond ~50 concurrent users |
| Encrypted-column migration corrupts data on partial failure | Backfill runbook stages: stop writes → backup → run migration → smoke-test decrypt → resume writes. Roll-back = restore from backup |
| Customer's egress proxy does TLS interception; our `httpx`/SDK calls fail with cert errors | `values.yaml: global.customCABundle.secretName` mounts a customer-supplied CA bundle into all pods; `httpx`/SDK clients honour `SSL_CERT_FILE` |
| Customer mandates FIPS 140-2/3-validated crypto modules | v1.0 documents the gap: Chainguard base + `cryptography` are FIPS-*friendly* but not FIPS-validated. Mitigation = roadmap commitment for v1.x; near-term workaround = customer reviews `cryptography` linkage against their internal FIPS-validated OpenSSL build |
| Customer requires image hosted in their internal registry; cosign signature lost on mirror | Document `cosign copy` (preserves signature) in the image-mirroring procedure; chart accepts `image.repository` override |
| Phase plan slips beyond week 8 | Buffer = week 8. Pre-agreed scope cuts in order: (1) drop Azure AD preset (keep Keycloak + Okta only), (2) drop CSV export (keep JSON only), (3) push P7 upgrade-in-place drill to v1.0.1 |

### Open questions (resolve during P0)

- **Which IdP is the pilot bank using?** Drives which preset gets the most polish in week 4. Default: Keycloak (easiest CI testing).
- **Which KMS backend does the pilot bank use?** Drives which ExternalSecret template gets the most testing. Default: AWS Secrets Manager (highest fintech share).
- **Chart name / namespace conventions?** Default `aifactory`/`aifactory` everywhere; parameterised.

## 8. Acceptance criteria (Epic close gate)

v1.0 Epic closes when ALL of the following are true:

- [ ] Container image scans clean (`trivy image` no HIGH/CRITICAL) and signature verifies (`cosign verify`)
- [ ] Image runs as uid 65532 on a PSS=restricted namespace without privilege escalation
- [ ] `helm install aifactory ./charts/aifactory` succeeds on a fresh kind cluster with `postgres.bundled=true`
- [ ] `helm install aifactory ./charts/aifactory` succeeds against an external Postgres 15 with `database.url` from a real `ExternalSecret`
- [ ] Install succeeds with `migrations.autoApply=false` (Alembic runs as a separate Helm Job by a privileged DBA role)
- [ ] OIDC login flow completes end-to-end against Keycloak in CI, with PKCE + state validation
- [ ] OIDC logout flow revokes server session and redirects to IdP `end_session_endpoint`
- [ ] User disabled in the IdP loses access within ≤15 min (JWT TTL boundary)
- [ ] JIT user provisioning creates `User` + `OrganizationMember` on first login
- [ ] `pg_dump` of a fresh install contains no plaintext credentials
- [ ] `pg_dump` → `pg_restore` round-trip preserves encrypted-column decryption via the current KMS root key
- [ ] `helm upgrade` from a seeded synthetic v0.x DB (plaintext credentials, no `kms_data_keys`) to v1.0 succeeds with no data loss; encrypted columns readable after upgrade
- [ ] `GET /api/audit/export?format=csv` and `format=json` return streams with valid hash-chain verification
- [ ] `POST /api/users/{id}/gdpr-erasure` deletes PII and preserves audit integrity (chain still verifies after erasure)
- [ ] `GET /metrics` returns Prometheus-format data on an authenticated scrape; cardinality bounded (route templates, not raw paths)
- [ ] structlog JSON output parses with `jq`
- [ ] SOC2 control mapping doc reviewed by at least one non-author reviewer against a checklist derived from the SOC2 CC1–CC9 controls list (external auditor preferred; internal peer with checklist acceptable for pilot)
- [ ] DPIA / data-flow diagram delivered (GDPR Article 35)
- [ ] Deployment runbook tested by someone other than the author against a fresh cluster
- [ ] Image-mirroring procedure (with `cosign copy`) documented and tested against a private registry

## 9. References

- Prior compliance audit: `guides/COMPLIANCE_AUDIT_2026-05.md`
- Current container baseline: `Dockerfile`, `docker-compose.yml`
- Multi-tenant data model: `apps/web-server/server/database/models.py`
- Audit log usage: `apps/web-server/server/services/audit_service.py`
- Auth middleware: `apps/web-server/server/auth.py`
- Provider factory (for v1.1 LiteLLM swap): `apps/backend/providers/factory.py`
