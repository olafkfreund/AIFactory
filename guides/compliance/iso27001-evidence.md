# ISO 27001 Annex A — AIFactory evidence map

> Living document. Maps each Annex A control AIFactory **directly evidences** to the technical artifact that demonstrates it. Controls AIFactory does NOT evidence are marked **operator responsibility** with a brief note.

## How to use this document

For a Stage 1 / Stage 2 ISO 27001 audit, your ISMS lead pairs each Annex A control with evidence from:

1. **Your organization's policies** (training, business continuity, etc.) — out of AIFactory's scope.
2. **AIFactory's technical implementation** — this document.
3. **Operator-supplied configuration** — your Helm values, Secret rotation runbooks, on-call schedule.

Each control entry below has three parts:
- **Control text** — verbatim or a close paraphrase of the Annex A wording.
- **AIFactory contribution** — what code, feature, or default in AIFactory demonstrates this.
- **Operator responsibility** — what you must add on top.

Coverage as of v1.1: **~31 controls directly evidenced** (Epic #35 9/9 shipped). ~80 of the 114 Annex A controls are organizational (policies, training, physical security, supplier relationships) and are out of AIFactory's scope by design.

## Scope statement

This document covers AIFactory as a self-hosted Kubernetes deployment using the Helm chart at `charts/aifactory/`. Cloud-hosted SaaS use of AIFactory is not in scope (no SaaS exists in v1.1).

---

## A.5 Information security policies

### A.5.1.1 Policies for information security

- **AIFactory contribution**: `SECURITY.md`, `README.md` security section, signed commits in `dev`/`main` branches.
- **Operator responsibility**: Your organization's overall ISMS policies. AIFactory's technical controls are evidence that your policy is enforced, not the policy itself.

---

## A.8 Asset management

### A.8.1.1 Inventory of assets

- **AIFactory contribution**: All assets are container images + Helm values; both are version-controlled in operator-owned git repos. `audit_signing_keys` table inventories the KMS-wrapped audit-anchor keys with version + creation timestamp.
- **Operator responsibility**: Your CMDB tracks the Kubernetes cluster, Postgres instance, KMS root key, and Object Storage bucket that host AIFactory.

### A.8.2.1 Classification of information

- **AIFactory contribution**: Three-tier classification on every `audit_logs` row (`public` / `internal` / `confidential`); classifiers in `audit_service.py` set the tier per action kind. The export endpoint accepts a `?max_classification=` filter for reviewer-scoped exports.
- **Operator responsibility**: Map your organization's data-classification taxonomy to AIFactory's three tiers in your data-handling SOP.

---

## A.9 Access control

### A.9.2.1 User registration and de-registration

- **AIFactory contribution**: OIDC SSO with JIT-provisioning (Epic #26 P3). New users created on first IdP login; de-registration via the IdP propagates within `OIDC_REFRESH_TOKEN_TTL` (default 15 min).
- **Partially implemented**: SAML 2.0 + SCIM 2.0 — Epic #35 #41. As of this document's first version, the security-foundation modules (PR #177) and `external_identities` schema (PR #178) are in `dev`; SAML routes + SCIM CRUD lands in PR-1b2/1b3 of #41. Until #41 closes, OIDC-only deployments fully evidence A.9.2.1; SAML-mandated banks see partial implementation.
- **Operator responsibility**: Connect your corporate IdP. Document the user-lifecycle SOP that maps "HR offboard" to "IdP suspend".

### A.9.2.5 Review of user access rights

- **AIFactory contribution**: `GET /api/admin/access-review?org=<id>` streams NDJSON of current OrgMembers with email, role, active, `joined_at`, `last_login_at` (Epic #35 #43 PR-1b4).
- **Operator responsibility**: Schedule a quarterly review. Run the export, distribute to organizational managers, capture sign-offs in your ISMS.

### A.9.4.1 Information access restriction

- **AIFactory contribution**: Per-org RBAC via `OrgMember.role` (`owner` / `admin` / `member` / `viewer`). Admin-only routes enforce `Depends(require_org_role("admin"))`.
- **Operator responsibility**: Map your job roles to AIFactory's four roles. Audit `org.member.role.change` events quarterly.

### A.9.4.2 Secure log-on procedures

- **AIFactory contribution**: OIDC PKCE flow (authlib); MFA enforced at the IdP. Local password login was deprecated pre-#26. JWT access tokens are 15-minute TTL with refresh-session model.
- **Operator responsibility**: Enforce MFA at your IdP. Configure session-timeout to match your policy.

### A.9.4.3 Password management system

- **AIFactory contribution**: No AIFactory-managed passwords (OIDC SSO only). Password storage is the IdP's responsibility.
- **Operator responsibility**: Use an MFA-enforcing IdP.

### A.9.4.4 Use of privileged utility programs

- **AIFactory contribution**: Scoped MCP API keys (Epic #35 #154) replace the host-wide admin token with per-developer `acw_` keys; mutating MCP routes are scope-gated.
- **Operator responsibility**: Document who has `acw_*` keys with admin scopes. Rotate quarterly.

### A.9.2 Privileged access management (tenant reconciler)

- **AIFactory contribution**: When Tenant Isolation Mode (Epic #35 #36) is enabled, the reconciler authenticates to Vault via a dedicated `aifactory-reconciler` AppRole with the minimum-needed `sys/policies/acl/aifactory-tenant-*` + `auth/kubernetes/role/aifactory-tenant-*` capabilities (it can MANAGE tenant policies but cannot READ tenant secrets). Per-tenant ServiceAccounts use IRSA (AWS) / Workload Identity (GCP/Azure) — never a shared cluster-wide cloud credential. See [tenant-isolation concept doc](../../docs/docs/concepts/tenant-isolation.md).
- **Operator responsibility**: Pre-create the `aifactory-reconciler` AppRole with the documented minimum capabilities. **Never** use a root token for the reconciler (documented anti-pattern). Rotate the AppRole secret per your KMS policy.

---

## A.10 Cryptography

### A.10.1.1 Policy on the use of cryptographic controls

- **AIFactory contribution**: All at-rest encryption uses `apps/web-server/server/crypto/` KMS abstraction. Five backends (Fernet for dev, AWS KMS / Vault Transit / Azure Key Vault / GCP KMS for production). Symmetric for data-at-rest; HMAC-SHA256 for audit anchors (Epic #35 #43); RSA-SHA256 for SAML assertion signing (Epic #35 #41).
- **Operator responsibility**: Document your KMS choice in your cryptographic-controls policy. Pick AWS KMS / Vault / Azure / GCP — Fernet local-key is dev-only.

### A.10.1.2 Key management

- **AIFactory contribution**: 
  - Data-encryption keys wrapped by KMS root, rotated via `RotationManager` (Epic #26 P2).
  - Audit-anchor signing keys versioned in `audit_signing_keys` table — KMS root rotation re-wraps without invalidating prior anchors (Epic #35 #43 PR-1a).
  - SAML SP signing cert rotation via `sp.x509certMulti` with overlap window (Epic #35 #41 design doc).
- **Operator responsibility**: Rotate the KMS root key per your crypto policy (typically annual). Document the rotation runbook for both data keys and audit-anchor keys.

---

## A.12 Operations security

### A.12.1.4 Separation of development, testing and operational environments

- **AIFactory contribution**: Helm-based deployment lets you `helm install -n aifactory-dev` / `-n aifactory-prod` with separate namespaces, separate Postgres databases, separate KMS roots. Per-namespace ServiceAccount + NetworkPolicy isolate the environments at the cluster level.
- **Operator responsibility**: Run separate Postgres + KMS for dev/test/prod. Don't reuse a single `values.yaml` across environments.

### A.12.3.1 Information backup

- **AIFactory contribution**: All state lives in Postgres + optional S3 workspace storage (Epic #35 #40). Both have well-documented backup paths (`pg_dump`, S3 versioning).
- **Operator responsibility**: Configure your Postgres backup cadence + retention. Test restores quarterly.

### A.12.4.1 Event logging

- **AIFactory contribution**: 
  - Structured logging via `structlog` (Epic #26 P6). One JSON line per event with request_id correlation.
  - Hash-chained audit log via `AuditLog` table (Epic #26 P5).
  - Signed daily audit-chain anchor (Epic #35 #43 — **closes v1.0 limitation #1**).
- **Operator responsibility**: Ship `stdout` logs to your aggregation pipeline (Loki / ELK / Splunk). Document log-retention period (default 13 months for audit; matches SOC2 12mo + buffer).

### A.12.4.2 Protection of log information

- **AIFactory contribution**:
  - `AuditLog.prev_hash` chains every row to its predecessor — chain break = tampering detected (Epic #26 P5.2).
  - `audit_anchors` daily HMAC sign of the chain head detects tampering even when DB admin re-computes the chain (Epic #35 #43).
  - The export interleaves anchors with rows; `verify_anchored_export()` provides an offline verifier helper.
- **Operator responsibility**: Run the access-review export + audit-anchor verification quarterly as part of your audit-log integrity SOP. Keep the KMS-wrapped audit-signing key separate from DB admin access.

### A.12.4.3 Administrator and operator logs

- **AIFactory contribution**: Every admin action (org member add/remove, role change, API key issuance, audit erasure) produces an `AuditLog` row tagged with `classification='confidential'`.
- **Operator responsibility**: Include the admin log in your quarterly review. Investigate any `audit.erasure` events.

### A.12.4.1 Audit of LLM calls (multi-provider deployments)

- **AIFactory contribution**: When LiteLLM gateway is enabled (Epic #35 #38, opt-in via `litellm.enabled=true`), all non-Claude LLM calls (OpenAI, Codex, Gemini, Ollama) are audited via LiteLLM's admin API: prompt, response, tokens, cost routed to `audit_hooks` table. Claude calls via Claude Agent SDK are covered by the existing chain-anchor audit mechanism (Epic #35 #43), but do NOT receive per-tenant budget enforcement, rate-limiting, or model allowlist enforcement in v1.1.
- **Known v1.1 limitation**: Claude calls bypass LiteLLM enforcement (scope revised after design review). Applies only to Claude; other providers fully gated. v1.2 closes via either (a) in-process Claude-SDK wrapper mirroring LiteLLM enforcement, or (b) LiteLLM Anthropic-format passthrough if upstream adds support (design doc §Scope).
- **Operator responsibility**: When `litellm.enabled=true`, monitor the audit hooks table for LLM spend & usage. Schedule a quarterly audit of per-tenant token spend. Document your per-tenant budget caps in your DPIA. For Claude calls, rely on external Anthropic billing dashboards until v1.2 adds enforcement.


### A.12.6.1 Management of technical vulnerabilities

- **AIFactory contribution**: CI (`.github/workflows/ci.yml`) runs Ruff lint, full test suite (~2400 tests), Helm lint + kubeconform, multi-arch container build with provenance attestations, dependency-update bot (Dependabot).
- **Operator responsibility**: Watch GitHub Releases (`olafkfreund/AIFactory`) for security advisories. Apply within your patch SLA.

---

## A.13 Communications security

### A.13.1.1 Network controls

- **AIFactory contribution**: `NetworkPolicy` template (`charts/aifactory/templates/networkpolicy.yaml`) restricts traffic to ingress + Postgres + KMS endpoints. gVisor opt-in for agent pods (Epic #35 #37) provides syscall-level isolation.
- **Operator responsibility**: Verify the rendered NetworkPolicy matches your cluster's CNI plugin. Validate by running `kubectl exec` from an unprivileged pod and confirming egress is blocked.

### A.13.1 Network segmentation (multi-tenant deployments)

- **AIFactory contribution**: Tenant Isolation Mode (Epic #35 #36) provisions per-Organization Kubernetes Namespace + ServiceAccount + default-deny NetworkPolicy + FQDN-based egress allowlist (Calico FQDN beta OR Cilium `CiliumNetworkPolicy`). Agent pods spawn into the tenant's namespace and cannot reach other tenants' workloads by construction. The Helm pre-install hook (`templates/pre-install-cni-probe.yaml`) hard-fails the install when neither Calico nor Cilium CRDs are present, so operators see CNI capability gaps at install time rather than first reconcile. Opt-in via `tenant.isolationEnabled=true`. See [tenant-isolation concept doc](../../docs/docs/concepts/tenant-isolation.md).
- **Operator responsibility**: Install Calico or Cilium as your cluster CNI. Enable `tenant.isolationEnabled=true` for multi-tenant deployments. Strongly consider also enabling `tenant.gatekeeperEnabled=true` (OPA sample policies that deny non-`aifactory-tenant-*` namespaces — closes the reconciler RBAC privilege concentration documented in the concept doc).

### A.13.2.1 Information transfer policies and procedures

- **AIFactory contribution**: All HTTP egress goes through `httpx` clients with TLS verification on; correlation IDs (`X-Request-ID`) propagated through every outbound call; OpenTelemetry tracing across HTTP / DB / agent subprocess (Epic #35 #42). **v1.2 #210** — LLM prompts can be PII-scrubbed BEFORE egress to the LLM vendor via `LITELLM_AUDIT_SCRUB_OUTBOUND=true` (deployment-wide) or per-provider `scrub_outbound=True`; closes the v1.1 "LLM sees plaintext PII" gap for opt-in orgs. The audit row's `details_json.prompt_outbound_scrubbed` boolean is the auditor's proof.
- **A.13.2 coverage for opt-in orgs is now FULL.** With scrubBeforeSend enabled, PII no longer leaves the AIFactory pod via the LLM-egress data flow.
- **Operator responsibility**: Terminate TLS at your ingress controller. Document the data flows in your DPIA. For PCI / high-sensitivity tenants, enable `LITELLM_AUDIT_SCRUB_OUTBOUND=true` and record the operator-sign-off in your compliance log.

---

## A.14 System acquisition, development and maintenance

### A.14.2.1 Secure development policy

- **AIFactory contribution**: Signed commits required on `main`; pre-commit hooks enforce Ruff + test runs; PRs require all CI checks green before merge; CODEOWNERS gates security-sensitive files.
- **Operator responsibility**: Document your fork's commit-signing policy if you maintain a private fork.

### A.14.2.5 Secure system engineering principles

- **AIFactory contribution**: Failure-safe contract everywhere — broken KMS / Redis / OTel / SAML never crashes the web pod (each integration wraps in try/except). Defense in depth: KMS + signed audit anchor + per-IdP collision guard.
- **Operator responsibility**: Inherit AIFactory's failure-safe defaults; don't disable error handling in your forks.

### A.14.2 PII redaction in LLM audit (Design considerations)

- **AIFactory contribution**: When LiteLLM gateway is enabled with the PII redactor module (Epic #35 #38), regular-expression patterns (SSN, email, phone) are redacted from `audit_hooks.prompt` and `audit_hooks.response` before storage, replacing with placeholders like `[REDACTED_SSN]` / `[REDACTED_EMAIL]` / `[REDACTED_PHONE]`. **v1.2 #210** adds a Luhn-validated credit-card pattern (`[REDACTED_CC]`) as a built-in — operators with PCI data no longer have to wire their own Luhn-checked regex via `extraRedactionPatterns`. The Luhn check eliminates the v1.1 false-positive problem (IPv4 CIDRs, code identifiers, hashes were corrupted by the original pre-Luhn naive pattern).
- **Resolved v1.1 limitation (v1.2 #210)**: PII redaction was AUDIT-ROW ONLY in v1.1 — the LLM itself received plaintext PII. v1.2 ships `LITELLM_AUDIT_SCRUB_OUTBOUND=true` (deployment-wide) / `OpenAICompatibleProvider(scrub_outbound=True)` (per-instance) which runs the same redactor on the prompt BEFORE the LLM API call. Opt-in by design (off by default) so existing v1.1 deployments see no behaviour change. Audit row records `details_json.prompt_outbound_scrubbed: true` when the pre-send pass actually changed the prompt — operators query that flag for "every call where PII left the LLM oblivious" reports.
- **Operator responsibility**: Document the chosen mode in your DPIA. For PCI / high-sensitivity tenants, enable `LITELLM_AUDIT_SCRUB_OUTBOUND=true` AND ensure your Data Processor Agreement with the LLM vendor (Anthropic, OpenAI, etc.) covers the residual case (e.g. operator-extra patterns that miss something the LLM still sees). For low-sensitivity tenants where prompt fidelity matters more than vendor-side oblivion, leave the flag off (v1.1 behaviour) and document the audit-only scope.


### A.14.2.8 System security testing

- **AIFactory contribution**: 14 CI acceptance jobs gate every PR (audit, backend, docker, evidence, frontend, helm, obs, oidc, postgres ×2, rmux, secrets + Build Docusaurus + Deploy to GitHub Pages). ~2400 backend tests including unit, integration, e2e.
- **Operator responsibility**: Run the same CI gates on your fork if you carry patches.

---

## A.16 Information security incident management

### A.16.1.7 Collection of evidence

- **AIFactory contribution**: NDJSON streaming export of the full audit log with interleaved signed anchors (Epic #35 #43). Offline verifier helper produces a `verified=True/False` answer plus per-failure line numbers.
- **Operator responsibility**: Document your incident-response runbook — who pulls the export, who runs the verifier, who countersigns the chain-of-custody.

---

## A.17 Information security aspects of business continuity management

### A.17.1.2 Implementing information security continuity

- **AIFactory contribution**: Stateless web pods (HPA-friendly), Postgres + S3 + Redis are externally managed. Multi-replica fan-out via Redis (Epic #35 #40) eliminates single-pod failure modes. Startup backfill of missed audit anchors after multi-day outages.
- **Operator responsibility**: Run multi-AZ Postgres + multi-region S3 if your RTO/RPO demands it.

---

## A.18 Compliance

### A.18.1.3 Protection of records

- **AIFactory contribution**: Audit-chain anchor closes the v1.0 limitation where a DB admin could rewrite the audit log without detection (Epic #35 #43 — see [audit-anchor concept doc](../../docs/docs/concepts/audit-anchor.md)).
- **Operator responsibility**: Keep the KMS-wrapped audit-signing key Secret separate from DB admin access. v1.2's external publication (S3 WORM / RFC 3161 / Sigstore) will remove this trust assumption.

### A.18.1.4 Privacy and protection of personally identifiable information

- **AIFactory contribution**: GDPR right-to-erasure endpoint (`POST /api/admin/users/{id}/erase`); erasure rewrites `details_json` + nulls `user_id` while preserving the audit chain (Epic #26 P5.5). PII columns (email, name) marked nullable in the schema so erasure leaves clean placeholders.
- **Operator responsibility**: Document your DPIA. Map your GDPR/CCPA-relevant data flows to AIFactory's User / OrgMember / AuditLog tables.

### A.18.1 Compliance with legal requirements (tenant decommissioning)

- **AIFactory contribution**: Tenant Isolation Mode's two-stage tear-down (Epic #35 #36) distinguishes between PII (deleted IMMEDIATELY on org soft-delete per GDPR Art. 17 "without undue delay") and infrastructure (30-day grace period configurable via `tenant.deletionGraceDays`, supports mistaken-delete recovery + legal-hold negotiation). Stage-1 (`Organization.deleted_at` set) nulls `User.email`/`User.name` for users with exclusive membership + hashes `user_id` in audit logs. Stage-2 (daily `tenant-teardown` CronJob after grace elapses, plus 24-hour dry-run preview window) deletes the namespace + S3 prefix (with `^orgs/[0-9a-f-]{36}/$` shape assertion) + Vault path. See [tenant-isolation concept doc](../../docs/docs/concepts/tenant-isolation.md) §tear-down.
- **Operator responsibility**: Set `tenant.deletionGraceDays` to match your data-retention policy (default 30 days; day 0 allowed but logs WARNING). Monitor stuck-terminating tenants via `SELECT org_id, reconcile_error FROM tenant_states WHERE deleted_at IS NOT NULL AND reconcile_error IS NOT NULL`. Document the PII-vs-infrastructure deletion distinction in your DPIA so auditors see both windows.

---

## Out of AIFactory's scope (operator-owned)

The following Annex A controls are entirely organizational and AIFactory does not directly evidence them. Documented here so an auditor can see we've thought about scope:

- **A.6 Organization of information security** — your ISMS roles, segregation of duties.
- **A.7 Human resource security** — background checks, training, NDAs.
- **A.11 Physical and environmental security** — datacenter, office, BYOD policies. (Your Kubernetes cluster's physical security is your IaaS provider's responsibility.)
- **A.15 Supplier relationships** — third-party risk management for your Postgres provider, KMS provider, OIDC provider, etc.
- **A.16 (mostly)** — incident response procedures, communications during incidents. AIFactory provides evidence collection but not the procedures.

---

## Maintenance

This document is updated whenever a new Epic shifts a control's status. Track changes via the git log:

```bash
git log --oneline guides/compliance/iso27001-evidence.md
```

Non-author review SHOULD precede every ISO 27001 audit. The author makes a PR; a reviewer who didn't write the controls confirms the AIFactory contribution claims by spot-checking the code paths.

## Maturity notes for ISO 27001 v2022

ISO 27001 v2022 reorganized Annex A from 14 categories (114 controls) into 4 themes (93 controls). This document follows the older 14-category structure because:
1. Operators with existing v2013-based ISMSes have an easier migration path.
2. The control IDs (A.9.2.5 etc.) are still recognised by all major auditing firms.
3. The v2022 → v2013 mapping is published by ISO; future versions of this doc will add v2022 cross-references.
