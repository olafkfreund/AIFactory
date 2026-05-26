# Threat Model — AIFactory v1.0

> Methodology: Microsoft STRIDE (Spoofing, Tampering, Repudiation,
> Information disclosure, Denial of service, Elevation of privilege).
> Scope: v1.0 architecture per `guides/plans/2026-05-24-aifactory-enterprise-v1-design.md`.
> Status: v1.0 (closing Epic #26).

## Scope

This threat model covers the AIFactory application as deployed via
the provided Helm chart on a customer-managed Kubernetes cluster.
**Out of scope**:

- The cloud provider's underlying SOC 2 inheritance (covered by
  AWS/Azure/GCP shared-responsibility model).
- The customer's identity provider's threat model (covered by
  Okta/Microsoft/Keycloak vendor documentation).
- LLM provider's threat model (covered by Anthropic's documentation).
- Physical security (covered by §CC6.7 of the SOC 2 evidence pack).

## Assets

| # | Asset | Confidentiality | Integrity | Availability |
| --- | --- | --- | --- | --- |
| A1 | OAuth tokens for upstream services | Critical | High | Low |
| A2 | LLM provider API keys | Critical | High | Medium |
| A3 | Audit log | Medium | **Critical** | Medium |
| A4 | User credentials / sessions | High | High | High |
| A5 | Per-org data keys | **Critical** | **Critical** | High |
| A6 | KMS root key (cloud-managed) | **Critical** | **Critical** | **Critical** |
| A7 | Application source + image signature | Medium | **Critical** | Low |
| A8 | User-supplied task prompts | High | Medium | Medium |

## STRIDE analysis

### S — Spoofing

| ID | Threat | Mitigation in v1.0 |
| --- | --- | --- |
| S1 | Attacker forges a JWT to impersonate a user | HS256 JWT with `JWT_SECRET` (≥256 bits); short access TTL (15min); refresh requires IdP `userinfo` re-validation (P3.4). |
| S2 | Attacker intercepts OIDC redirect, steals auth code | PKCE S256 + `state` + `nonce` (P3.1, P3.2). Tests: `test_pkce_state_tamper_rejected`. |
| S3 | Attacker spoofs IdP via DNS poisoning | Operator's DNSSEC + TLS cert validation. Application honors `customCABundle` for corporate CAs. |
| S4 | Attacker impersonates a service inside the cluster | NetworkPolicy default-deny + `ServiceAccount` with `automountServiceAccountToken=false` (P4.3). |
| S5 | Attacker spoofs `X-Request-ID` to confuse audit log | DOCUMENTED LIMITATION: correlation IDs are NOT authoritative; the auditable `user_id` is JWT-verified separately. |

### T — Tampering

| ID | Threat | Mitigation |
| --- | --- | --- |
| T1 | DB admin mutates audit log to cover tracks | Hash chain (P5.2). `verify_chain` detects mutations. External air-gapped verifier (`python -m server.audit verify-chain`). **LIMITATION**: chain has no signed external anchor — DB admin who can re-compute can also re-chain. v1.1 fix: signed external anchor (Epic #35). |
| T2 | Encrypted column ciphertext modified at rest | AES-256-GCM auth tag detects mutation; decrypt raises `InvalidTag` (test: `test_encrypted_string_rejects_tampered_ciphertext`). |
| T3 | Helm chart image swapped post-build | Image signed via cosign (P0); chart accepts mirrored images with signature preserved via `cosign copy` (documented in image-mirroring runbook). |
| T4 | Migration runs out-of-order via direct DB access | Alembic version table + `alembic upgrade head`-enforced linear history. |
| T5 | Application config tampered via ConfigMap edit | Helm RBAC: `ServiceAccount` doesn't have ConfigMap write permission. ConfigMap checksum annotation on Deployment forces pod restart on edit (visibility). |

### R — Repudiation

| ID | Threat | Mitigation |
| --- | --- | --- |
| R1 | Admin denies performing a sensitive action | Hash-chained audit log (P5). Cannot be deleted without breaking the chain. |
| R2 | User denies OIDC login | OIDC IdP's own audit log (operator inherits). |
| R3 | API token use can't be attributed | API tokens are per-user (P3 model); audit log records `user_id`. |

### I — Information disclosure

| ID | Threat | Mitigation |
| --- | --- | --- |
| I1 | OAuth tokens visible in `pg_dump` | EncryptedString TypeDecorator (P2.1). Backup contains ciphertext only. |
| I2 | KMS plaintext keys visible in app memory dump | Per-org data keys cached in-process (`DataKeyManager`); rotation invalidates on `rotated_at` change. Process memory protection is the OS's responsibility. |
| I3 | Plaintext credentials in logs | Default structlog config DOES NOT log credential columns; tests gate this (`tests/secrets/test_p2_encrypted_string.py::test_no_plaintext_in_stored_bytes`). |
| I4 | Audit log details_json leaks PII | Bounded by GDPR redaction list (P5.5 `_redact_details_json`). Operator extends per data-classification policy. |
| I5 | Side-channel via Prometheus metrics labels | Cardinality cap (P6.3) means `handler` is route template, not raw path. User IDs NEVER appear in metric labels. |
| I6 | Browser CSRF on token endpoint | SameSite=Lax cookies; PKCE on OIDC flow; bearer auth on API requests. |
| I7 | TLS-intercepting corporate proxy | `global.customCABundle` mounts customer CA; tests gate (`test_custom_ca_bundle_is_trusted_by_pod`). |

### D — Denial of service

| ID | Threat | Mitigation |
| --- | --- | --- |
| D1 | Slowloris on `/api/auth/oidc/callback` | Uvicorn timeout settings; NetworkPolicy egress allowlist limits attack surface. |
| D2 | Database connection exhaustion | SQLAlchemy connection pool; resource limits on app pod. |
| D3 | KMS rate-limit exhaustion via login storm | DataKeyManager caches per-org keys for 60s; bulk logins amortize the cost. |
| D4 | Audit log table fills disk | Daily retention job (P5.6) deletes rows past `retention_until`. |
| D5 | Webhook flood on `/api/health` | Standard K8s readiness probe pattern; ingress controller rate-limits if configured. |
| D6 | Single-replica saturation | DOCUMENTED LIMITATION for v1.0 — operators with >50 concurrent users plan for v1.1's multi-replica + Redis pub/sub. |

### E — Elevation of privilege

| ID | Threat | Mitigation |
| --- | --- | --- |
| E1 | Container breakout via syscall | PSS=restricted: dropped ALL caps, RuntimeDefault seccomp, runAsNonRoot 65532, readOnlyRootFilesystem. CIS Kubernetes Benchmark §5.7. |
| E2 | Compromised app reads other pods' Secrets | NetworkPolicy + RBAC: ServiceAccount can't list/get Secrets across namespaces. |
| E3 | OIDC claim injection bypasses role mapping | Server validates JWT signature + claims; doesn't trust client-supplied role claims directly. Mapping is server-side from `groups`. |
| E4 | SQL injection via task name | SQLAlchemy parameterized queries throughout. |
| E5 | Path traversal on file upload | File upload routes (existing) validate paths against project base directory. |
| E6 | Helm chart upgrade introduces malicious template | Operator pulls signed image (cosign verify); chart RBAC limits ServiceAccount blast radius. |

## Documented limitations (load-bearing for SOC 2 evidence)

These threats are NOT mitigated in v1.0; the operator must accept
or compensate.

| ID | Limitation | Compensating control |
| --- | --- | --- |
| L1 | Audit chain unsigned (T1) | Operator runs `verify-chain` daily + archives result to immutable storage. |
| L2 | Revocation latency ≤ 15 min (S1) | Operator can force restart pod for instant invalidation of in-process userinfo cache. |
| L3 | LLM provider sees user-supplied prompts (I-class) | Operator's policy enforcement on `task_prompts` (v1.1 PII filter via LiteLLM). |
| L4 | Single replica = single point of availability failure (D6) | Operator runs canary in second cluster + DNS failover. |
| L5 | FIPS modules not validated | Operator's compliance function accepts at procurement time. |
| L6 | No back-channel logout (S1) | Forced JWT TTL ≤ 15min bounds the window. |

## Attack tree examples

### "Read another tenant's audit log"

```
GOAL: Read another tenant's audit log
├─ via API
│   ├─ Compromise tenant admin user (S1, S2) — mitigated: PKCE+TTL
│   ├─ JWT forgery (S1) — mitigated: HS256 secret rotation
│   └─ Direct DB access — operator-prevented (network policy / RBAC)
├─ via backup
│   ├─ Compromise backup bucket (out of v1.0 scope; operator's S3 IAM)
│   └─ pg_restore + bypass auth — would still need KMS access for decrypt
└─ via memory
    └─ Process memory dump (out of scope; OS / cloud provider)
```

### "Tamper an audit log row"

```
GOAL: Tamper an audit log row to hide an action
├─ DB write
│   ├─ Compromise DB admin (T1) — chain detects, verifier catches in <24h drill
│   └─ Direct UPDATE — chain detects (test_tampered_row_breaks_chain)
├─ Bypass chain
│   ├─ Re-chain forward (T1 limitation) — requires DB write AND code knowledge — DOCUMENTED LIMITATION
│   └─ Delete + reinsert — chain detects (prev_hash mismatch)
└─ Backup-only
    └─ Modify backup file — restore requires KMS unwrap; modified rows fail verify
```

## Review cadence

| Trigger | Action |
| --- | --- |
| Architecture change | Re-walk STRIDE; update assets table. |
| New external integration | Add to scope; identify trust boundary crossings. |
| Security incident | Update affected STRIDE rows; tighten compensating controls. |
| Major release | Full re-review. |

## Reviewer signoff

| Role | Name | Date |
| --- | --- | --- |
| Author | Olaf Krasicki-Freund | 2026-05-25 |
| Security review | _TBD via PR review_ | _TBD_ |
