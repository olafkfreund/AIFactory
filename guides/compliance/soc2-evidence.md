# SOC 2 Trust Service Criteria — AIFactory evidence map

> Living document. Maps each SOC 2 Trust Service Criteria (TSC, 2017 framework with 2022 Points of Focus) that AIFactory **directly evidences** to the technical artifact that demonstrates the control. Criteria AIFactory does NOT directly evidence are marked **operator responsibility**.

## How to use this document

For a SOC 2 Type 1 (point-in-time) or Type 2 (period-of-time) audit, your security lead pairs each TSC with evidence from:

1. **Your organization's policies** — change management, vendor management, BCDR plans (out of AIFactory's scope).
2. **AIFactory's technical implementation** — this document.
3. **Operator-supplied configuration + sustained operation evidence** — your Helm values, KMS rotation logs, on-call records over the audit window.

Each criterion entry has three parts:

- **Criterion** — the TSC title plus the 2022 Points of Focus that AIFactory evidence speaks to.
- **AIFactory contribution** — code, feature, default behaviour, or generated artifact.
- **Operator responsibility** — what you must add on top to claim full coverage.

Coverage as of v1.1: **Security (CC1-CC9), Availability (A1), and Confidentiality (C1) all at least partially evidenced**. Privacy (P1-P8) is partial — AIFactory ships GDPR Art. 17 erasure + PII redaction, but full Privacy TSC requires organizational notice + consent procedures out of scope.

Cross-references in this document:
- [`audit-trail.md`](../operations/audit-trail.md) — hash-chain + signed-anchor operator runbook.
- [`threat-model.md`](../security/threat-model.md) — STRIDE-per-component threat analysis.
- [`kms-rotation-runbook.md`](../operations/kms-rotation-runbook.md) — KMS root-key rotation procedure (data keys + audit-anchor keys).

## Scope statement

This document covers AIFactory as a self-hosted Kubernetes deployment using the Helm chart at `charts/aifactory/`. Cloud-hosted SaaS use is out of scope (no SaaS exists in v1.1). The audit window is the operator-chosen reporting period, typically 6-12 months.

---

## CC1: Control environment

### CC1.1, CC1.2, CC1.3 — Governance, leadership, ethics

- **AIFactory contribution**: Source available at `https://github.com/olafkfreund/AIFactory` under MIT/GPL-3.0 dual licence; `SECURITY.md` documents the disclosure policy; CODEOWNERS gates security-sensitive paths; signed commits required on `main`/`dev`.
- **Operator responsibility**: Your code of conduct, leadership oversight cadence, employee acknowledgements. Document who in your org "owns" the AIFactory deployment.

### CC1.4, CC1.5 — Competence + accountability

- **AIFactory contribution**: PR descriptions reference the AIFactory `superhuman` spec for every feature so reviewers see the rationale + acceptance criteria. The `_bmad/` planning suite captures intent for the period.
- **Operator responsibility**: Your role descriptions, training records, performance reviews for the engineers operating the cluster.

---

## CC2: Communication and information

### CC2.1 — Internal communication about responsibilities

- **AIFactory contribution**: `CLAUDE.md` + `_bmad/` plus the Docusaurus concept docs at `docs/docs/concepts/` explain how the system works for new on-call engineers.
- **Operator responsibility**: Document who in your org responds to AIFactory alerts; capture in your on-call rota.

### CC2.2 — External communication of objectives

- **AIFactory contribution**: GitHub Releases publish a changelog, SBOM, and cosign-signed images per release. Security advisories use GHSA.
- **Operator responsibility**: Inform downstream customers of your AIFactory version + patch cadence in your Trust Centre / DPA.

### CC2.3 — Customer + supplier obligations

- **AIFactory contribution**: AIFactory is a self-hosted dependency. No customer data flows to the AIFactory project owners.
- **Operator responsibility**: Map AIFactory in your vendor inventory + Data Processor Agreement matrix. List Anthropic / OpenAI / Bedrock / Vertex / Vault / KMS as sub-processors of AIFactory's LLM and crypto flows.

---

## CC3: Risk assessment

### CC3.1, CC3.2 — Identifying + analysing risks

- **AIFactory contribution**: Threat model published per major component at [`guides/security/threat-model.md`](../security/threat-model.md). Each closed epic ships a "Documented limitations" section so risks are tracked as living documents (see ISO 27001 doc for closed-loop on v1.0 limitations).
- **Operator responsibility**: Run your own risk assessment over your AIFactory deployment — review the threat model at least annually + after each major upgrade.

### CC3.3 — Fraud risk

- **AIFactory contribution**: All admin actions write an `audit_logs` row with `classification='confidential'`; chain + anchor detect retroactive tampering (Epic #35 #43).
- **Operator responsibility**: Schedule a quarterly access-review export + audit-anchor verification (see [`audit-trail.md`](../operations/audit-trail.md)).

### CC3.4 — Change-related risk

- **AIFactory contribution**: CI runs 14 acceptance suites + Ruff + Helm lint + kubeconform on every PR; required reviews block merge until green.
- **Operator responsibility**: Your change-management policy. Helm `--dry-run` + `--diff` before every upgrade; capture sign-off.

---

## CC4: Monitoring activities

### CC4.1, CC4.2 — Ongoing + separate evaluation

- **AIFactory contribution**: Structured logs (`structlog` Epic #26 P6) emit JSON with request_id correlation; Prometheus metrics + OTel traces (Epic #35 #42) cover HTTP + DB + agent subprocess; Grafana dashboard at `guides/observability/grafana-aifactory.json` provides a turnkey monitoring surface.
- **Operator responsibility**: Wire Loki/ELK/Splunk to ingest logs. Wire Prometheus to scrape `/metrics`. Wire OTel collector to your tracing backend (Tempo, Jaeger, Datadog). Define on-call alerts on the dashboard's panels.

---

## CC5: Control activities

### CC5.1, CC5.2, CC5.3 — Selection, deployment, technology controls

- **AIFactory contribution**: Helm-templated NetworkPolicy + ServiceAccount + per-pod resource limits; gVisor optional (Epic #35 #37); per-tenant namespace + ServiceAccount under Tenant Isolation Mode (Epic #35 #36); OPA Gatekeeper sample policies ship in `charts/aifactory/policies/`.
- **Operator responsibility**: Enable the controls — `tenant.isolationEnabled=true`, `tenant.gatekeeperEnabled=true`, gVisor runtimeclass. Document the selection rationale in your control matrix.

---

## CC6: Logical and physical access controls

### CC6.1 — Logical access

- **AIFactory contribution**:
  - OIDC SSO with PKCE; JWT access tokens 15-min TTL with refresh-session model (Epic #26 P3).
  - SAML 2.0 with SP-init + IdP-init flows + replay defence (Epic #35 #41).
  - SAML Single Logout (v1.2 #209) propagates IdP-side disable to AIFactory within milliseconds.
  - SCIM 2.0 CRUD for Users + Groups (Epic #35 #41 PR-1b3) gives the IdP push-provisioning + de-provisioning into AIFactory's `external_identities` table.
- **Operator responsibility**: Enforce MFA at the IdP. Connect SCIM. Configure short JWT TTL if you have a high-security profile.

### CC6.2 — User provisioning + de-provisioning

- **AIFactory contribution**: SCIM 2.0 user/group lifecycle from the IdP into `external_identities` + `OrgMember`. Quarterly access-review NDJSON export at `GET /api/admin/access-review?org=<id>` (Epic #35 #43 PR-1b4).
- **Operator responsibility**: Schedule the quarterly export to your audit evidence drive. Capture organisational-manager sign-offs.

### CC6.3 — Role-based access

- **AIFactory contribution**: `OrgMember.role` four-tier RBAC (`owner` / `admin` / `member` / `viewer`); admin routes gate via `Depends(require_org_role("admin"))`; tenant isolation enforces namespace-level segregation when enabled.
- **Operator responsibility**: Map your job roles to the four AIFactory roles. Audit `org.member.role.change` events.

### CC6.6 — Boundary protection

- **AIFactory contribution**: Helm `NetworkPolicy` template restricts traffic to ingress + Postgres + KMS; tenant isolation adds default-deny + FQDN egress allowlist (Calico FQDN policy or Cilium `CiliumNetworkPolicy`).
- **Operator responsibility**: Install Calico or Cilium. Verify the rendered NetworkPolicy with `kubectl exec` from an unprivileged pod.

### CC6.7 — Data-in-transit protection

- **AIFactory contribution**: All outbound HTTP uses `httpx` with TLS verification ON; OTel propagates `traceparent` across HTTP / DB / subprocess; v1.2 #210 ships `LITELLM_AUDIT_SCRUB_OUTBOUND=true` so PII can be scrubbed BEFORE LLM egress (closes the v1.1 plaintext-PII-to-LLM gap).
- **Operator responsibility**: Terminate TLS at your ingress. Enable scrubBeforeSend for PCI/PHI tenants.

### CC6.8 — Detection + prevention of malicious software

- **AIFactory contribution**: Container images built reproducibly with multi-arch provenance attestations; cosign-signed via GitHub OIDC; SBOM (SPDX) attached as an attestation. See [`guides/operations/image-mirroring.md`](../operations/image-mirroring.md) for chain-of-custody preservation when mirroring.
- **Operator responsibility**: Run admission controller (Sigstore policy-controller / Kyverno verifyImages / OPA cosign template) that enforces signature-presence at pod start. Run image-scanning (Trivy, Snyk, Wiz) on the mirrored images.

---

## CC7: System operations

### CC7.1 — Detect security events

- **AIFactory contribution**:
  - Distributed OTel tracing (Epic #35 #42) emits spans for every HTTP + DB + agent subprocess so anomalies are visible across services.
  - Audit-chain anchor (Epic #35 #43) detects retroactive tampering even by a DB admin.
  - LiteLLM audit (Epic #35 #38) records every LLM call (prompt, response, tokens, cost) for non-Claude providers; v1.2 #207 wraps Claude SDK calls too.
- **Operator responsibility**: Build SIEM alert rules on the audit + log streams. Define what "abnormal" means for your org (geographic anomalies, burst usage, sensitive-prompt classifiers, etc).

### CC7.2 — System monitoring (the heart of SOC 2 for SaaS-like services)

- **AIFactory contribution**:
  - **Structured logging** — every request emits a structlog JSON line tagged with `request_id`.
  - **Metrics** — `/metrics` exposes Prometheus text exposition; route templates (not raw paths) on the `handler` label so cardinality is bounded.
  - **Tracing** — OTel auto-instrumentation across FastAPI + httpx + asyncpg + subprocess (Epic #35 #42).
  - **Audit** — every authenticated action writes an `audit_logs` row with hash-chain (Epic #26 P5.2) + signed daily anchor (Epic #35 #43) per [`audit-trail.md`](../operations/audit-trail.md).
- **Operator responsibility**: Centralise the logs. Define SLOs on the metrics. Configure alert routing. Run the anchor verifier quarterly.

### CC7.3 — Evaluate security events

- **AIFactory contribution**: NDJSON streaming export of the full audit log with interleaved signed anchors at `GET /api/audit/export`; offline verifier helper at `python -m server.audit verify-chain`.
- **Operator responsibility**: Define your incident-response playbook: who pulls the export, who runs the verifier, who countersigns chain-of-custody.

### CC7.4 — Respond to security incidents

- **AIFactory contribution**: GDPR Art. 17 erasure endpoint (`POST /api/admin/users/{id}/erase`) for cleanup of leaked PII. Tenant Isolation Mode's two-stage tear-down (Epic #35 #36) immediately deletes per-tenant PII while keeping infrastructure for forensic recovery.
- **Operator responsibility**: Your IR plan: containment, eradication, recovery, lessons learned. Run a tabletop quarterly.

### CC7.5 — Recovery from incidents

- **AIFactory contribution**: Stateless web pods + Postgres as the only persistent state; backup/restore drill at `scripts/drills/backup-restore.sh` (CI runs `--dry-run` on every PR).
- **Operator responsibility**: Document RTO/RPO. Run a live restore in a staging cluster quarterly. Multi-AZ Postgres if RTO < 4h.

---

## CC8: Change management

### CC8.1 — Authorised change management

- **AIFactory contribution**:
  - CI pipeline (`.github/workflows/ci.yml`) runs Ruff + ~2400 backend tests + Helm lint + kubeconform + multi-arch image build with provenance.
  - CODEOWNERS auto-routes security-sensitive PR reviews.
  - Cosign signatures + SBOM + SLSA-3 provenance on every released image.
  - Database migrations apply forward-only via Alembic; downgrade SQL exists but operators must manually verify schema-safety.
- **Operator responsibility**: Your change-approval workflow. Helm `--dry-run` + `--diff` in pre-prod before every prod upgrade. Capture approval evidence in your change-management system.

---

## CC9: Risk mitigation

### CC9.1 — Mitigate risk of business disruption

- **AIFactory contribution**: Multi-replica web pods supported via Redis pub/sub fan-out (Epic #35 #40); HPA-friendly; stateless web pods recover from pod loss without data loss.
- **Operator responsibility**: Configure HPA min replicas >= 2. Run Postgres + Redis externally with their own HA story.

### CC9.2 — Vendor management

- **AIFactory contribution**: Dependencies tracked in `apps/backend/requirements.txt` and `apps/frontend-web/package.json`; Dependabot proposes upgrades; SBOM attestation on every image lists vendor + version.
- **Operator responsibility**: Maintain a vendor risk inventory. AIFactory itself is one vendor; the LLM provider (Anthropic/OpenAI/etc), Postgres provider, KMS provider, OIDC provider are sub-processors.

---

## A1: Availability

### A1.1 — Capacity planning

- **AIFactory contribution**: Resource requests + limits set per pod in the Helm chart defaults; HPA template ships. Prometheus metrics expose request rate, latency, error rate, queue depth for capacity planning.
- **Operator responsibility**: Run load tests for your peak; tune HPA min/max replicas; size Postgres for your projected concurrency.

### A1.2 — Backups + recovery

- **AIFactory contribution**: All persistent state in Postgres + optional S3 workspace storage (Epic #35 #40). Backup/restore drill at `scripts/drills/backup-restore.sh` exercises the round-trip; CI verifies the script stays runnable. Stateless agents — no agent-local state survives pod restart.
- **Operator responsibility**: `pg_dump --format=custom` daily, retain >= 90 days, off-site replication for DR. Test restores quarterly. S3 versioning + cross-region replication for workspace data.

### A1.3 — Recovery testing

- **AIFactory contribution**: Three drill scripts ship in `scripts/drills/` (`backup-restore.sh`, `upgrade-in-place.sh`, `image-mirroring.sh`). All three run in `--dry-run` on every PR so the drill procedure cannot bit-rot relative to the implementation.
- **Operator responsibility**: Run live drills (not just dry-runs) in your staging environment on a documented cadence. Capture the runtime + outcome.

---

## C1: Confidentiality

### C1.1 — Identify + maintain confidential information

- **AIFactory contribution**: Three-tier classification on every `audit_logs` row (`public` / `internal` / `confidential`); classifiers in `audit_service.py` set the tier per action kind. Export endpoint accepts `?max_classification=` for reviewer-scoped exports.
- **Operator responsibility**: Map your organisation's data-classification taxonomy onto the three tiers. Document in your data-handling SOP.

### C1.2 — Disposal of confidential information

- **AIFactory contribution**:
  - GDPR Art. 17 erasure rewrites `details_json` + nulls `user_id` while preserving the audit chain (Epic #26 P5.5).
  - Tenant Isolation Mode two-stage tear-down (Epic #35 #36): stage-1 immediately nulls PII; stage-2 deletes namespace + S3 prefix + Vault path after grace period.
  - At-rest data wrapped by KMS (Fernet for dev; AWS KMS / Vault Transit / Azure Key Vault / GCP KMS for production) per [`kms-rotation-runbook.md`](../operations/kms-rotation-runbook.md).
- **Operator responsibility**: Configure `tenant.deletionGraceDays` to match your retention policy. Choose a production KMS (Fernet local-key is dev-only). Document key-rotation cadence per [`kms-rotation-runbook.md`](../operations/kms-rotation-runbook.md).

### C1 (supplementary) — PII redaction at rest + in transit

- **AIFactory contribution**:
  - LiteLLM PII redactor (Epic #35 #38) regex-redacts SSN/email/phone/CC from prompt + response BEFORE persisting in `audit_hooks`. v1.2 #210 adds Luhn-validated CC pattern (eliminates IPv4/hash false positives).
  - v1.2 #210 `LITELLM_AUDIT_SCRUB_OUTBOUND=true` scrubs PII BEFORE the LLM API call so the LLM vendor never sees raw PII. Audit row records `details_json.prompt_outbound_scrubbed: true`.
- **Operator responsibility**: Enable scrubBeforeSend for high-sensitivity tenants. Document in your DPIA which mode each tenant uses.

---

## Documented limitations

This section documents SOC 2-relevant gaps in v1.1 so auditors see them before they find them.

1. **External audit-anchor publication (CC7.2)** — v1.1 stores the daily HMAC anchor in Postgres next to the chain it protects. A DB admin who can also access the KMS-wrapped signing key could rewrite both. v1.2 #208 ships per-tenant anchors + external publication (S3 WORM, RFC 3161 timestamping, or Sigstore Rekor) to remove this trust assumption. Current evidence relies on KMS-secret separation between DB admin and audit-key holder.
2. **JWT statelessness window (CC6.1)** — JWT access tokens remain valid for their 15-minute TTL even after the user is disabled at the IdP. v1.2 #209 SAML Single Logout closes the IdP-pushed disable side; refresh-session model still leaves a 15-min residual. High-security operators should set `JWT_ACCESS_TTL_SECONDS=300` and accept the latency penalty.
3. **Claude calls bypass LiteLLM enforcement in v1.1 (CC6.7, CC7.2)** — Claude Agent SDK calls in v1.1 are NOT routed through LiteLLM, so the per-tenant budget cap + model allowlist + scrubBeforeSend do not apply to Claude. v1.2 #207 wraps Claude SDK calls. Operators relying on Claude as primary provider should treat this as a documented exception until #207 closes.
4. **Privacy TSC partial coverage** — AIFactory implements GDPR Art. 17 erasure + PII redaction + scrubBeforeSend, but full SOC 2 Privacy TSC (P1-P8) requires organisational notice + consent + onward transfer disclosures that live in your Privacy Notice + DPA, not in code. AIFactory cannot make you SOC 2 Privacy-certified by itself.
5. **Recovery testing depends on operator drill execution (CC9.1, A1.3)** — AIFactory ships executable drill scripts and CI-verifies they remain runnable; operators must actually run them live in staging. CI's `--dry-run` is necessary but not sufficient SOC 2 evidence.

---

## Maintenance

This document is updated whenever an epic shifts a criterion's status. Track changes via git:

```bash
git log --oneline guides/compliance/soc2-evidence.md
```

Non-author review SHOULD precede every SOC 2 audit. Pair this document with the auditor's TSC selection during scope-setting so any criterion not listed here can be marked "not in scope" with documented rationale.
