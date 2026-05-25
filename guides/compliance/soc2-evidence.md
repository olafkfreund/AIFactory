# SOC 2 Type II Evidence Pack — AIFactory v1.0

> Audience: SOC 2 auditors, compliance teams, customer procurement reviewers.
> Framework: AICPA Trust Services Criteria (2017), revised 2022.
> Status: v1.0 (closing Epic #26).

## How to read this document

Each Trust Services Criterion (CC1 through CC9 + the
common-criteria-aware Availability A1 + Confidentiality C1) is
mapped to:

1. **Auditable artifact** — a specific file path / CI workflow /
   runtime behavior the auditor can verify.
2. **Test evidence** — the automated test or CI gate that proves
   the control operates as designed.
3. **Limitation / out-of-scope** — what v1.0 explicitly does NOT
   claim, with the v1.1 plan.

Every artifact below is reproducible from a clean clone of this
repository at the v1.0 tag.

---

## CC1: Control Environment

### CC1.1 — Commitment to integrity and ethical values

| Artifact | Evidence |
| --- | --- |
| `CONTRIBUTING.md` | Code of conduct, contributor expectations. |
| `LICENSE` (AGPL-3.0) | Licensing terms public. |
| Commit sign-off (`git commit -s`) | Documented requirement in `guides/CONTRIBUTING.md`. |

### CC1.2 — Board / management oversight

| Artifact | Evidence |
| --- | --- |
| `guides/plans/2026-05-24-aifactory-enterprise-v1-design.md` | Documented design decisions + scope-cut policy (§7). |
| Epic #26 + sub-issues #27-#34 | All design + scope changes tracked publicly. |

### CC1.4 — Personnel competence

Out-of-scope for v1.0 — this is the operator's HR control, not the
product's. AIFactory provides RBAC (P3 OIDC `groupToRole` mapping)
so operators can grant least-privilege access; **competence
verification is the operator's responsibility**.

---

## CC2: Communication and Information

### CC2.1 — Internal communication of objectives

| Artifact | Evidence |
| --- | --- |
| `README.md` | Product purpose statement. |
| `guides/plans/2026-05-24-aifactory-enterprise-v1-design.md` | v1.0 objectives + acceptance criteria. |

### CC2.2 — External communication

| Artifact | Evidence |
| --- | --- |
| `guides/` | All operator-facing documentation public. |
| GitHub Issues | Bug reports, feature requests, security advisories. |

### CC2.3 — Security disclosure

| Artifact | Evidence |
| --- | --- |
| `SECURITY.md` | Vulnerability disclosure procedure (v1.1 ships dedicated; v1.0 = GitHub Security Advisories). |

---

## CC3: Risk Assessment

### CC3.1 — Identification of risks

| Artifact | Evidence |
| --- | --- |
| `guides/security/threat-model.md` | STRIDE threat model over v1.0 architecture. |
| `guides/plans/2026-05-24-aifactory-enterprise-v1-design.md` §7 | Risk register with mitigation per risk. |

### CC3.2 — Risk response

| Artifact | Evidence |
| --- | --- |
| `guides/security/threat-model.md` (mitigations column) | Per-threat mitigation linked to source. |
| `guides/operations/audit-trail.md` (threat-model section) | Documented limitations of the hash chain (signed-anchor = v1.1). |

---

## CC4: Monitoring Activities

### CC4.1 — Ongoing + periodic monitoring

| Artifact | Evidence |
| --- | --- |
| `apps/web-server/server/observability/metrics.py` | `/metrics` Prometheus endpoint. |
| `guides/observability/grafana-aifactory.json` | 7-panel ops dashboard. |
| `guides/operations/observability.md` | Operator runbook for ongoing monitoring. |
| CI's `obs-acceptance` job | Tests gate cardinality cap + structured-log format on every PR. |

### CC4.2 — Evaluation + communication of deficiencies

| Artifact | Evidence |
| --- | --- |
| `apps/web-server/server/services/audit_chain.py` | Tamper-detection on the audit log itself. |
| `python -m server.audit verify-chain` | External verifier for periodic integrity audit. |

---

## CC5: Control Activities

### CC5.1 — Selection + development of control activities

This entire document is the evidence. Each control below maps to a
specific artifact + test.

### CC5.2 — Technology general controls

| Artifact | Evidence |
| --- | --- |
| `charts/aifactory/values.yaml` (podSecurityContext / containerSecurityContext) | PSS-restricted defaults: runAsNonRoot, dropped ALL caps, RuntimeDefault seccomp. |
| `charts/aifactory/templates/networkpolicy.yaml` | Default-deny + explicit 443 egress allowlist. |
| `tests/helm/test_p4_helm.py::test_pss_restricted_security_contexts` | CI gate on every PR. |

### CC5.3 — Policies and procedures

All operator procedures documented in `guides/operations/` and
`guides/deployment/`.

---

## CC6: Logical and Physical Access Controls

### CC6.1 — Logical access security software, infrastructure, and architectures

| Control | Artifact |
| --- | --- |
| Identity | OIDC SSO (P3) → `apps/web-server/server/oidc/` + `guides/operations/oidc-setup.md`. |
| Authentication | PKCE + state + nonce (P3). Short JWT TTL (15min access / 8h refresh). |
| Authorization | `OrgMember.role` from claim-mapping (`APP_OIDC_GROUP_TO_ROLE`). |
| Service-to-service | API token + `TokenAuthMiddleware`. |
| Audit | Every `/api/auth/oidc/*` action emits a hash-chained audit log entry. |

### CC6.2 — User authentication

| Artifact | Evidence |
| --- | --- |
| OIDC PKCE flow (P3) | `tests/oidc/test_p3_oidc.py::test_login_callback_pkce_roundtrip`. |
| State tamper rejection | `test_pkce_state_tamper_rejected`. |
| Revocation within TTL | `test_user_disabled_in_idp_revoked_within_ttl`. |

### CC6.3 — Role-based access (least privilege)

| Artifact | Evidence |
| --- | --- |
| `OrgMember.role` model | `apps/web-server/server/database/models.py`. |
| Claim-to-role mapping | `APP_OIDC_GROUP_TO_ROLE` env (P3.3). |
| Test | `tests/oidc/test_p3_oidc.py::test_jit_provisions_user_and_org_member`. |

### CC6.6 — Logical access termination

| Artifact | Evidence |
| --- | --- |
| GDPR Art. 17 erasure | `POST /api/users/{id}/gdpr-erasure` (P5.5). |
| Audit-chain re-verification post-erasure | `tests/audit/test_p5_audit.py::test_erasure_deletes_pii_but_chain_still_verifies`. |
| OIDC `end_session_endpoint` redirect | `POST /api/auth/oidc/logout` (P3.5). |

### CC6.7 — Restriction of physical access

Out-of-scope for v1.0 — physical access is the cloud provider's
SOC2 inheritance (AWS / Azure / GCP). AIFactory's responsibility
ends at the container.

### CC6.8 — Unauthorized access detection

| Artifact | Evidence |
| --- | --- |
| `audit_logs` table | Every security-relevant action logged with hash chain. |
| Prometheus 5xx alert | `guides/observability/grafana-aifactory.json` panel 3. |

---

## CC7: System Operations

### CC7.1 — System operations monitoring

| Artifact | Evidence |
| --- | --- |
| `/api/health` endpoint | Liveness + readiness probes. |
| `/metrics` Prometheus | 5 standard metric families + custom audit/OIDC labels. |
| Grafana dashboard | Pre-built; operator imports once. |

### CC7.2 — System operations: incident response

| Artifact | Evidence |
| --- | --- |
| `guides/operations/encrypted-secrets-dr.md` | 5 disaster scenarios with recovery steps. |
| `guides/operations/audit-trail.md` | Verification procedure if tampering suspected. |
| Correlation IDs (P6.2) | Every log line + outbound HTTP carries `X-Request-ID` for tracing. |

### CC7.3 — System change management

| Artifact | Evidence |
| --- | --- |
| GitHub PR workflow (CONTRIBUTING.md) | All changes via PR. |
| CI gates | 10+ acceptance jobs on every PR (P0-P6 all green = mergeable). |
| Alembic migrations | Versioned schema changes; forward-only documented. |

### CC7.4 — Configuration management

| Artifact | Evidence |
| --- | --- |
| `charts/aifactory/values.yaml` + `values.schema.json` | Schema-validated config; `helm lint --strict` enforces. |
| ConfigMap + Secrets separation | Helm chart enforces. |

### CC7.5 — Recovery from incidents

| Artifact | Evidence |
| --- | --- |
| `guides/deployment/runbook.md` § Backup/restore | Documented + drill script. |
| `scripts/drills/backup-restore.sh` | Executable drill. |
| `guides/operations/encrypted-secrets-dr.md` | Per-scenario recovery. |

---

## CC8: Change Management

### CC8.1 — Authorization, design, development, testing, approval, implementation of changes

| Stage | Artifact |
| --- | --- |
| Authorization | GitHub Issues + Epic #26 design spec. |
| Design | `guides/plans/*-design.md`. |
| Development | Branch-per-feature; commit sign-off. |
| Testing | 10+ acceptance suites (P0 through P7) on every PR. |
| Approval | PR review + CI gates. |
| Implementation | Merge → automated deploy via Helm. |

---

## CC9: Risk Mitigation

### CC9.1 — Identification + selection of risks; mitigations

See `guides/security/threat-model.md` STRIDE + Epic #26 design
spec §7 risk register.

### CC9.2 — Vendor / third-party risk

| Artifact | Evidence |
| --- | --- |
| `apps/web-server/requirements.txt` | All Python deps pinned. |
| Trivy CVE scan in CI (P0) | Image scan on every release. |
| `cosign` signatures (P0) | Verifiable supply chain. |

---

## A1: Availability

| Control | Artifact |
| --- | --- |
| Capacity planning | HPA template (`charts/aifactory/templates/hpa.yaml`); v1.0 ships disabled (single replica) — documented limitation. |
| Backups | `pg_dump` via cloud-provider managed Postgres; drill script `scripts/drills/backup-restore.sh`. |
| Disaster recovery | `guides/operations/encrypted-secrets-dr.md`. |
| Recovery testing | Quarterly drill recommended; `scripts/drills/backup-restore.sh --dry-run` for CI continuous validation. |

---

## C1: Confidentiality

| Control | Artifact |
| --- | --- |
| Data classification | `guides/compliance/dpia-data-flow.md` PII inventory. |
| Encryption at rest | `EncryptedString` TypeDecorator (P2.1) + per-org `kms_data_keys` (P2.2) + 5 KMS backends (P2.4). |
| Encryption in transit | TLS terminated at Ingress (operator's reverse proxy). |
| Key rotation | `python -m server.crypto rotate-root` + `guides/operations/kms-rotation-runbook.md` (P2.5/P2.6). |
| Audit of confidential access | Hash-chained `audit_logs` (P5). |

---

## Documented limitations of v1.0

These are **explicit non-claims**. The auditor should not infer
these controls are in place.

| Limitation | Where documented | v1.1 plan |
| --- | --- | --- |
| Audit chain has no signed external anchor | `guides/operations/audit-trail.md` § threat model | Signed-anchor mode (Epic #35). |
| Revocation latency ≤ 15 min (= access-token TTL) | `tests/oidc/test_p3_oidc.py::test_user_disabled_in_idp_revoked_within_ttl` | Back-channel logout (v1.1). |
| FIPS 140-2/3 modules NOT validated | Epic #26 design spec §7 | v1.x roadmap. |
| Distributed tracing not built-in | `guides/operations/observability.md` | OpenTelemetry (v1.1, Epic #35). |
| Single replica only | `charts/aifactory/values.yaml` `replicaCount: 1` + Helm chart README | Redis pub/sub + multi-replica (v1.1). |
| LLM call audit deferred to provider gateway | Epic #26 design spec §4 last paragraph | LiteLLM gateway audit (v1.1). |

---

## Reviewer signoff

| Role | Name | Date | Notes |
| --- | --- | --- | --- |
| Author | Olaf Krasicki-Freund | 2026-05-25 | v1.0 SOC2 evidence pack drafted. |
| Reviewer | _TBD via PR review_ | _TBD_ | Required for SOC2 acceptance gate. |
