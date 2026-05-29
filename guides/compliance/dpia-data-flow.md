# Data Protection Impact Assessment — data flow + PII inventory

> Audience: Data Protection Officers, Privacy Engineers, and SOC2/ISO27001 lead auditors mapping AIFactory deployments against GDPR + similar privacy regimes (UK GDPR, CCPA, LGPD, PIPEDA).
> Scope: Self-hosted AIFactory v1.1 deployed via the Helm chart at `charts/aifactory/`. SaaS / multi-tenant hosted use is out of scope (no such offering in v1.1).
> Companion docs: [`soc2-evidence.md`](./soc2-evidence.md), [`iso27001-evidence.md`](./iso27001-evidence.md), [`../security/threat-model.md`](../security/threat-model.md).

## How to use this document

This DPIA template covers the **AIFactory technical layer**. Your full DPIA additionally needs:

1. Your **organisational lawful-basis decisions** for each processing purpose (consent vs legitimate-interest vs contract vs legal-obligation).
2. Your **data-subject rights workflow** (response SLA, request-intake form, identity verification).
3. Your **vendor sub-processor disclosures** — the LLM provider (Anthropic, OpenAI, Bedrock, Vertex, Ollama-on-internal), the KMS provider, the IdP, the Postgres host. AIFactory is one sub-processor; it inherits a chain.
4. Your **risk assessment + residual-risk acceptance** signed by your DPO.

What AIFactory provides: the technical mechanisms below, plus the data-flow diagram + records of processing template + records of erasure proof.

## Records of processing (Art. 30)

GDPR Art. 30 requires a record of processing activities. Use the rows below as a starting point for your Art. 30 ROPA entry; complete the operator-specific cells (controller name, retention, sub-processors).

| Processing activity                          | Category of data subject     | Categories of personal data                        | Recipients                                      | Retention                                                  | Lawful basis (operator-set)      |
| -------------------------------------------- | ---------------------------- | -------------------------------------------------- | ----------------------------------------------- | ---------------------------------------------------------- | -------------------------------- |
| User authentication (OIDC/SAML SSO)          | Employees, contractors       | Email, display name, IdP `sub`, login timestamps   | IdP (corporate Okta / Azure AD / etc)           | Until offboarding + 13 mo audit-log retention              | Contract / legitimate interest    |
| Task / spec authoring                        | Employees                    | Free-form prompt text — may include PII pasted by user | LLM provider, AIFactory cluster, audit log     | Spec retained per project policy; audit row 13 months     | Legitimate interest               |
| LLM call (Claude / OpenAI / Bedrock / etc)   | Indirect (data subjects whose data the user pastes) | Prompt body, response body, tokens, cost          | LLM provider, `audit_hooks` table              | LLM-vendor-determined; audit row 13 months                | Legitimate interest + DPA terms   |
| Workspace artifact storage                   | Indirect                     | Files written by agent (may contain PII)          | S3 (Epic #35 #40) or PVC                       | Per task workspace; deleted on tenant tear-down            | Legitimate interest               |
| Audit logging                                | Employees                    | User ID, action, request body fingerprint, IP, UA | Audit-log DB (Postgres)                        | 13 months (configurable; SOC2 ⩾ 12 mo)                    | Legal obligation / legitimate interest |
| Tenant-level usage metrics                   | Aggregated                   | Anonymised counts (calls, tokens, latency)        | Prometheus + Grafana                           | Per Prometheus retention (default 15 days)                | Legitimate interest               |

This table feeds your Art. 30 Records of Processing Activities. AIFactory ships executable evidence for the **categories of data + recipients + retention** rows; the operator owns the **purposes + lawful basis** rows.

## Lawful basis

GDPR Art. 6 enumerates six lawful bases; AIFactory deployments typically rely on a mix:

| Processing                          | Most-common Art. 6 basis           | Notes                                                                                                  |
| ----------------------------------- | ---------------------------------- | ------------------------------------------------------------------------------------------------------ |
| Employee SSO authentication          | (b) contract                       | Employment relationship requires identity authentication to corporate systems.                         |
| Workplace productivity AI agents     | (f) legitimate interest            | Document the LIA (Legitimate Interest Assessment); offer opt-out via your IT request channel.          |
| Audit log + intrusion-detection     | (c) legal obligation / (f) LI      | Many regulators (FCA, BaFin, HIPAA, etc.) mandate audit; otherwise legitimate interest in security.    |
| Customer-data-in-prompts (B2B SaaS) | Per-customer DPA                   | Your customer is the controller, you are the processor — your DPA dictates lawful basis chain.         |
| Marketing experiments               | (a) consent                        | Out of typical AIFactory scope; if you wire it in, manage consent in your CDP.                          |

AIFactory does not make a lawful-basis selection on your behalf. The mechanism for opt-out at the tenant boundary is `Organization.deleted_at` + Tenant Isolation Mode tear-down (Epic #35 #36); for the individual data subject, it is GDPR Art. 17 erasure.

## Data inventory — where PII lives

Per-row + per-store enumeration of every place a PII byte can exist in an AIFactory deployment.

### Persistent stores

| Store                        | Table / object                                            | PII fields                                                        | Encryption-at-rest                                            | Retention default       |
| ---------------------------- | --------------------------------------------------------- | ----------------------------------------------------------------- | ------------------------------------------------------------- | ----------------------- |
| Postgres                     | `users`                                                   | `email`, `name`, `external_id` (IdP `sub`)                        | KMS-wrapped via `apps/web-server/server/crypto/`              | Until tenant tear-down  |
| Postgres                     | `external_identities` (SAML/SCIM v1.1)                    | `name_id`, `email`, `display_name`, IdP attributes                | KMS-wrapped                                                   | Per SCIM lifecycle      |
| Postgres                     | `audit_logs`                                              | `user_id`, `details_json` (may include PII), IP, UA               | KMS-wrapped; chained + signed                                 | 13 months               |
| Postgres                     | `audit_hooks` (LiteLLM Epic #35 #38)                      | `prompt`, `response` — PII-redacted before write                  | KMS-wrapped; SSN/email/phone/CC redacted to `[REDACTED_*]`    | 13 months               |
| Postgres                     | `tenant_states` (Tenant Isolation Epic #35 #36)           | `org_id`, deletion windows — no individual PII                    | KMS-wrapped                                                   | Per org lifecycle       |
| Object store (S3 / GCS / Azure Blob — opt-in Epic #35 #40) | `s3://<bucket>/orgs/<org_id>/workspaces/<task>/...` | Files the agent wrote — may contain PII the user pasted in        | Bucket-side SSE (KMS or AES256)                               | Per task workspace      |
| Vault                        | `aifactory/data/orgs/<org_id>/...` (Tenant Isolation)     | Per-org secrets — typically API keys, not PII                     | Vault Transit                                                 | Per tenant lifecycle    |

### Transient stores

| Store                                | PII scope                                                 | Notes                                                                                              |
| ------------------------------------ | --------------------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| Agent-pod memory + ephemeral FS      | Whatever the agent loaded for the task                    | Wiped on pod restart; no persistence to node disk because rootfs is read-only + emptyDir for `/tmp`. |
| Redis (multi-replica fan-out Epic #35 #40) | WebSocket session correlation IDs — no PII payloads | Redis stores only routing metadata; never the message body.                                        |
| LLM provider memory                  | Prompt + response                                         | Governed by the provider's DPA; AIFactory cannot inspect the vendor's retention.                   |
| stdout / journald                    | structlog JSON — may carry PII if explicitly logged       | Operator must scrub. AIFactory's default formatters do NOT log user prompts.                       |

## Data-flow diagram

The diagram below shows every PII-touching code path in a v1.1 deployment. Boundaries marked `[TLS]` are HTTPS with verified TLS; boundaries marked `[mTLS]` are mutual-TLS inside the cluster mesh (operator-installed); boundaries marked `[KMS-wrap]` are encrypted with a KMS-wrapped DEK.

```mermaid
flowchart LR
    classDef pii fill:#fde7e7,stroke:#a13030,stroke-width:1px
    classDef trust fill:#eaf3fb,stroke:#1f4e79,stroke-width:1px
    classDef wrap fill:#e8f3e6,stroke:#2a6b1f,stroke-width:1px

    User([End user<br/>browser]):::trust
    IdP([Corporate IdP<br/>OIDC / SAML]):::trust
    Ingress[Ingress / WAF<br/>TLS termination]:::trust
    Web[web-server pod<br/>FastAPI]:::trust
    Agent[agent-spawner pod<br/>per task]:::trust
    LLM([LLM provider<br/>Anthropic / OpenAI / etc]):::trust
    LiteLLM[LiteLLM gateway<br/>opt-in #38]:::trust
    PG[(Postgres<br/>KMS-wrapped):::wrap]:::pii
    S3[(S3 / GCS / Azure Blob<br/>workspaces #40):::wrap]:::pii
    Vault[(Vault<br/>per-org secrets):::wrap]:::wrap
    Logs[(structlog stdout)]:::trust
    Prom[(Prometheus):::trust]
    OTel[(OTel collector):::trust]
    Audit[(audit_logs<br/>chain + anchor):::wrap]:::pii

    User -->|"1. login [TLS]"| Ingress
    Ingress -->|"2. forward"| Web
    Web -->|"3. OIDC/SAML PKCE [TLS]"| IdP
    IdP -->|"4. id_token / SAML assertion"| Web
    Web -->|"5. KMS-wrap PII + persist"| PG

    User -->|"6. task prompt [TLS]"| Ingress
    Ingress --> Web
    Web -->|"7. spawn agent"| Agent
    Agent -->|"8a. Claude SDK direct"| LLM
    Agent -->|"8b. non-Claude via LiteLLM"| LiteLLM
    LiteLLM -->|"9. PII-redact, optional scrub-before-send"| LLM

    Agent -->|"10. workspace files [KMS-wrap]"| S3
    Agent -->|"11. tenant secret read [TLS]"| Vault
    Web -->|"12. audit row [KMS-wrap, chained]"| Audit
    LiteLLM -->|"13. audit_hooks (PII-redacted)"| PG

    Web --> Logs
    Web --> Prom
    Web --> OTel
    Agent --> Logs
    Agent --> OTel
```

Annotations:

- Boxes shaded red carry PII at rest.
- Boxes shaded green are KMS-wrapped.
- Edges 8a + 8b are the egress boundary where PII leaves AIFactory's trust zone for the LLM vendor. With v1.2 #210 `LITELLM_AUDIT_SCRUB_OUTBOUND=true`, edge 8b is PII-scrubbed BEFORE the LLM sees it; edge 8a (Claude direct) currently is not scrubbed (closed by v1.2 #207).
- Edges 12 + 13 write to the audit subsystem; the chain + anchor mechanism (Epic #35 #43) makes them tamper-evident.

## Cross-region + cross-border data flows

AIFactory does not enforce regional routing — that is the operator's responsibility through cluster placement + LLM-provider region selection. Common patterns:

| Tenant region        | Cluster region       | LLM-provider region                                       | DPA considerations                                                              |
| -------------------- | -------------------- | --------------------------------------------------------- | ------------------------------------------------------------------------------- |
| EU                   | EU (Frankfurt, Ireland) | Anthropic EU residency / Azure OpenAI EU / Bedrock EU      | Document SCCs for any US-headquartered provider; rely on EU residency where available. |
| UK                   | UK                   | Anthropic UK or EU; Azure OpenAI UK                       | UK GDPR; adequacy decision with EU still active.                                |
| US                   | US                   | Any                                                       | State-by-state: CCPA, CPRA, VCDPA, CPA, CTDPA, UCPA, etc.                       |
| APAC                 | Singapore / Sydney   | Region-local providers preferred; document any spillover  | PDPA Singapore, Privacy Act AU, APPI Japan.                                     |

Operator action: pin the LLM provider region in `LITELLM_API_BASE` or the SDK-specific region env var. AIFactory will route every call to whatever the operator configures.

## Data-retention periods

| Data class                         | Default retention             | Override mechanism                                                    | Rationale                                                                                |
| ---------------------------------- | ----------------------------- | --------------------------------------------------------------------- | ---------------------------------------------------------------------------------------- |
| Audit log rows                     | 13 months                     | `AUDIT_RETENTION_DAYS` env var                                        | SOC2 needs >= 12 mo; one-month buffer for delayed exports.                               |
| Tenant infrastructure              | 30 days after `deleted_at`    | `tenant.deletionGraceDays` Helm value                                 | Mistaken-delete recovery + legal-hold negotiation; min 0 days (immediate) allowed.       |
| Tenant PII (user emails, names)    | Immediate on `deleted_at`     | Not configurable — GDPR Art. 17 "without undue delay"                | Privacy-by-design — PII never enters the grace window.                                   |
| Agent-spawned files in workspace    | Per task lifecycle            | Configurable per task                                                 | Workspace is task-scoped; files are intermediate artifacts not durable records.         |
| Prometheus metrics                 | 15 days (Prometheus default)  | Prometheus config                                                     | Aggregated, non-PII; longer retention via Thanos / Mimir if needed.                      |
| OTel traces                        | Per operator's backend        | Tracing-backend config (Tempo, Jaeger, Datadog)                       | Operator-chosen, typically 7-30 days.                                                    |
| LLM provider memory                | Per provider DPA              | None at AIFactory layer                                               | Pin to providers with zero-retention or short-retention modes for PCI/PHI tenants.        |

## Data-subject rights

| Right                       | GDPR article | AIFactory mechanism                                                                                          | Operator workflow                                                                  |
| --------------------------- | ------------ | ------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------- |
| Access                      | Art. 15      | `GET /api/users/{id}/data-export` returns NDJSON of user + audit rows.                                       | Identity-verify the request, run the endpoint, deliver as secure download.        |
| Rectification               | Art. 16      | `PATCH /api/users/{id}` updates email + name; SCIM 2.0 from IdP also propagates.                            | Confirm via IdP first; let SCIM propagate.                                         |
| Erasure / "right to be forgotten" | **Art. 17** | `POST /api/admin/users/{id}/erase` rewrites `details_json` + nulls `user_id` while preserving the audit chain. | Within 30 days of valid request; document fulfilment in your DSR log.            |
| Restriction                 | Art. 18      | Set `User.is_active=false`; user cannot log in but data persists.                                            | Use during dispute investigation.                                                  |
| Portability                 | Art. 20      | Same export endpoint as Art. 15.                                                                             | Provide in machine-readable format (NDJSON satisfies the requirement).             |
| Object                      | Art. 21      | Disable account at IdP + delete via Art. 17 mechanism.                                                       | Document basis-for-refusal if invoking the compelling-legitimate-interest exemption. |
| Automated decision-making   | Art. 22      | AIFactory itself does NOT make automated decisions about humans. The LLM provider might, per their DPA.      | Disclose any downstream-of-AIFactory automated-decision use in your Privacy Notice. |

The Art. 17 erasure mechanism preserves the audit-chain integrity — see [`../operations/audit-trail.md`](../operations/audit-trail.md) for the chain mechanics. Auditors can verify a row was erased without breaking the chain.

## Sub-processor disclosure template

Include in your DPA exhibit, adjusted to your concrete stack:

| Sub-processor                | Purpose                                       | Data categories          | Region(s)                    | Transfer mechanism (if outside EU) |
| ---------------------------- | --------------------------------------------- | ------------------------ | ---------------------------- | ---------------------------------- |
| Anthropic / OpenAI / Bedrock | LLM inference                                 | Prompt + response        | Operator-chosen              | SCCs + provider DPA                |
| AWS / GCP / Azure (cluster)  | Kubernetes hosting                             | All in-cluster state     | Operator-chosen              | SCCs + provider DPA                |
| AWS KMS / Vault / Azure KV / GCP KMS | Key management                          | KMS-wrapped DEKs         | Operator-chosen              | SCCs + provider DPA                |
| Corporate IdP                | Authentication                                | Identity attributes      | Operator-chosen              | Per IdP DPA                        |
| Optional: Datadog / Splunk / etc | Observability                            | structlog + traces       | Operator-chosen              | Per provider DPA                   |

## Risk register

| Risk                                                                       | Likelihood | Impact | Mitigation                                                                                                                            | Residual risk                                                                  |
| -------------------------------------------------------------------------- | ---------- | ------ | ------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------ |
| User pastes raw PII into a prompt that goes to LLM provider.               | High       | Medium | v1.2 #210 scrubBeforeSend regex-redacts before egress; UI warning banner on copy-paste; user training.                                | LLM still sees content the regex misses; rely on provider zero-retention DPA. |
| DB admin retroactively rewrites audit log to cover up a breach.            | Low        | High   | Hash chain + signed daily anchor (Epic #35 #43); KMS-key separation from DB admin.                                                    | v1.2 #208 external publication closes the residual.                            |
| Cross-tenant data leakage via shared K8s namespace.                        | Medium     | High   | Tenant Isolation Mode (Epic #35 #36) per-org namespace + NetworkPolicy + Vault path; opt-in via `tenant.isolationEnabled=true`.       | None if isolation enabled + tested via the negative test in `tests/tenant_isolation/`. |
| Stale IdP session after offboarding lingers in JWT for up to 15 minutes.   | Medium     | Medium | v1.2 #209 SAML Single Logout propagates IdP-side disable; short JWT TTL configurable.                                                 | Stateless JWT minimum residual; operator can shorten TTL.                      |
| Backup archive copied to insecure storage.                                 | Low        | High   | `pg_dump` runs encrypted; operator wraps in additional crypto before upload. Drill script verifies KMS-wrap survives round-trip.       | Operator-managed; document in your backup SOP.                                  |

## Auditor checklist

For your DPO sign-off or external auditor walk-through:

- [ ] Records of processing (Art. 30) entries copied + operator-completed for each processing activity above.
- [ ] Lawful basis documented per processing activity.
- [ ] Sub-processor list disclosed to data subjects via Privacy Notice update.
- [ ] DSR workflow defined for Access / Rectification / Erasure with named owners + SLAs.
- [ ] Tenant Isolation Mode enabled if running multi-tenant.
- [ ] scrubBeforeSend enabled for any tenant in scope of PCI / PHI / pseudonymisation rules.
- [ ] KMS-key separation between DB admin role + audit-anchor key access verified.
- [ ] Backup + restore drill rehearsed within the audit window (not just CI dry-run).
- [ ] Threat model [`../security/threat-model.md`](../security/threat-model.md) reviewed within last 12 months.

## Maintenance

Update this document whenever:

1. A new persistent store of PII is added (new table, new bucket, new cache).
2. A retention default changes.
3. A new sub-processor is introduced.
4. The lawful-basis taxonomy changes in your jurisdiction.

```bash
git log --oneline guides/compliance/dpia-data-flow.md
```

Pair with the threat model in `guides/security/threat-model.md` — the DPIA tells you WHERE PII is; the threat model tells you what could go wrong with it.
