# AIFactory threat model — STRIDE per component

> Audience: Security architects, threat-modelling reviewers, and SOC2 / ISO27001 / SOC2-equivalent auditors evaluating AIFactory's defence-in-depth posture.
> Scope: Self-hosted AIFactory v1.1, Helm chart at `charts/aifactory/`. SaaS hosting is out of scope.
> Companion docs: [`../compliance/soc2-evidence.md`](../compliance/soc2-evidence.md), [`../compliance/dpia-data-flow.md`](../compliance/dpia-data-flow.md), [`../operations/audit-trail.md`](../operations/audit-trail.md).
> Methodology: STRIDE (Microsoft) — Spoofing, Tampering, Repudiation, Information disclosure, Denial of service, Elevation of privilege — applied per architectural component.

## How to use this document

1. **Annual review.** Walk through every component table; mark each control as "verified for this audit window" or "needs re-test"; capture in your security backlog.
2. **Post-incident.** When a real incident hits, the matching STRIDE row should already enumerate the attack path; if it does not, add it and re-publish.
3. **Pre-release.** Before merging a major architectural change, add or update the affected component table in this document as part of the PR.

The threat-actor model assumed throughout:

- **External attacker** — has internet access to your ingress, no credentials, may have leaked an old JWT.
- **Authenticated tenant user** — valid SSO login, member of one organization, wants to read another tenant's data.
- **Malicious insider with DB admin** — operations engineer who can directly query Postgres + read KMS secrets they were granted, but not those they were denied.
- **Compromised LLM provider** — the upstream LLM API responds with malicious tool-call output trying to exfiltrate or escalate.

## Components in scope

| Component                | Trust boundary it crosses                                | Source                                                        |
| ------------------------ | -------------------------------------------------------- | ------------------------------------------------------------- |
| **Web pod**              | Internet ↔ cluster                                       | `apps/web-server/server/` (FastAPI)                            |
| **Agent pod**            | Cluster ↔ task workspace ↔ LLM provider                  | `apps/backend/` (Claude SDK + provider abstraction)            |
| **LiteLLM gateway**      | Cluster ↔ LLM providers (non-Claude in v1.1)             | `charts/aifactory/charts/litellm/` (Epic #35 #38)              |
| **Audit subsystem**      | Web pod ↔ Postgres ↔ KMS                                 | `apps/web-server/server/services/audit_service.py` + chain    |
| **Tenant namespace**     | Per-org K8s namespace ↔ cluster control plane            | `apps/web-server/server/services/tenant_reconciler.py` (Epic #35 #36) |

---

## Web pod

The FastAPI process that terminates user sessions, issues JWTs, serves the REST + WebSocket API.

### S — Spoofing

| Threat                                                                                  | Control                                                                                                                                                                  | Reference                                  |
| --------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------ |
| Attacker presents a forged JWT to impersonate a user.                                   | JWT signed with operator-rotated HS256/RS256 key; signature verified on every request via `Depends(get_current_user)`. Asymmetric option recommended for high-security. | `core/auth.py`, FastAPI dep tree           |
| Attacker replays a captured SAML assertion to log in as the victim.                     | `SamlReplayCache` rejects re-use of `AssertionID` / `Response.ID` within the assertion's `NotOnOrAfter` window.                                                          | Epic #35 #41 + design doc                  |
| Attacker injects a fake OIDC `id_token` via a man-in-the-middle on the IdP callback.    | OIDC PKCE; state parameter HMAC-bound to session; only the redirect-URI configured with the IdP is accepted.                                                              | Epic #26 P3                                |
| Attacker pretends to be the IdP-side SCIM service.                                      | SCIM endpoint requires Bearer token rotated per IdP; per-IdP token never reused; rate-limited.                                                                            | Epic #35 #41 PR-1b3                        |

### T — Tampering

| Threat                                                                                | Control                                                                                                            | Reference                            |
| ------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------ | ------------------------------------ |
| Attacker tampers with the JWT to escalate role.                                       | Role lives in `OrgMember.role` not the JWT; lookup happens on each request.                                        | RBAC dependency in `core/security.py`|
| Attacker manipulates a request body to bypass server-side validation.                 | Pydantic schema validation on every endpoint; type coercion on inputs.                                              | FastAPI per-endpoint                  |
| Attacker tampers with at-rest config (e.g. ConfigMap edit).                           | Helm + GitOps-friendly; operator should run Argo/Flux to detect drift.                                              | Operator process                      |
| Attacker tampers with response between web pod and browser.                           | TLS at ingress; HSTS header; CSP enforced server-side.                                                              | `core/middleware.py`                  |

### R — Repudiation

| Threat                                                                | Control                                                                                                  | Reference                          |
| --------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------- | ---------------------------------- |
| User denies having performed an action (e.g. created an admin role). | `audit_logs` row with `user_id` + `kind` + `details_json`; hash chain + daily anchor prevents retroactive denial. | Epic #35 #43, audit-trail doc      |
| Admin denies having issued an API key.                                | `api_keys.issuer_user_id` + audit row tagged `confidential` for `api_key.create`.                        | `services/api_key_service.py`      |

### I — Information disclosure

| Threat                                                                                              | Control                                                                                                                                                                              | Reference                                |
| --------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------- |
| Attacker reads another tenant's data via a known endpoint.                                          | Every authenticated route resolves `org_id` from the JWT scope + filters DB queries by `org_id` server-side. SQL never trusts client-supplied `org_id`.                              | `core/security.py::require_org_role`     |
| Attacker reads logs containing PII.                                                                 | structlog default formatter does not log prompt bodies; operator must scrub if they explicitly add `log.info("prompt", body=...)`.                                                   | `observability/structlog_setup.py`        |
| Attacker scrapes `/metrics` to learn business numbers.                                              | `METRICS_SCRAPE_TOKEN` env var enables Bearer-required `/metrics`; route templates (not raw paths) avoid cardinality explosion that could leak per-org request volumes via path labels. | `observability/install_metrics`           |
| Stack-trace leakage on unexpected exception.                                                        | FastAPI exception handlers strip stack-traces in non-debug mode; structlog captures full trace server-side.                                                                          | `core/exception_handlers.py`              |

### D — Denial of service

| Threat                                                                              | Control                                                                                                       | Reference                          |
| ----------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------- | ---------------------------------- |
| Attacker floods the login endpoint to lock out legitimate users.                    | Operator-installed rate-limit at ingress (e.g. NGINX `limit_req`, Kong, Envoy); IdP itself rate-limits.        | Operator                           |
| Attacker sends extremely large request bodies.                                      | FastAPI `max_request_body` configurable; Uvicorn `--limit-max-requests` rotates worker.                       | `core/app.py`                       |
| Attacker triggers infinite-recursion / heavy query.                                 | DB query timeouts; per-request CPU budget enforced via async timeout.                                          | `services/*`                       |
| Connection exhaustion against backing services (Postgres, Redis, OTel).             | Async connection pools with bounded size; each integration wraps in try/except so one slow service ≠ web crash. | Per-service init                   |

### E — Elevation of privilege

| Threat                                                                                       | Control                                                                                                                                          | Reference                                  |
| -------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------ |
| `member` user calls an admin-only endpoint.                                                  | Every admin route gates via `Depends(require_org_role("admin"))`.                                                                                | `core/security.py`                          |
| User in org-A logs in and reads org-B data.                                                  | JWT scope resolves to exactly one `OrgMember`; cross-org access requires explicit `org_id` param + role check at that org.                       | `core/security.py`                          |
| User issues themselves an API key with broader scopes than they have.                        | API-key issuance checks `OrgMember.role` and refuses scopes the user does not already hold.                                                       | Epic #35 #154                              |
| Attacker exploits SQL injection.                                                             | All queries go through SQLAlchemy parametrised statements; no string-concat into SQL.                                                            | Codebase invariant                          |
| Attacker exploits SSRF via a user-supplied URL.                                              | Httpx clients default to deny private IP ranges + metadata service IPs unless explicitly allowlisted per integration.                            | `core/http_clients.py`                      |

---

## Agent pod

The per-task Claude Agent SDK subprocess that executes the task plan.

### S — Spoofing

| Threat                                                                                 | Control                                                                                            | Reference                          |
| -------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------- | ---------------------------------- |
| Spawned agent claims a different `org_id` than the spawning user.                      | Agent inherits `org_id` from the spawning JWT scope; not configurable by the agent prompt itself.  | `agents/coder.py` orchestration    |
| Compromised LLM responds with output pretending to be a system message.                | Tool-output parsing is strict; the agent only executes tools whose schema validates.               | `auto_claude_tools.py`             |

### T — Tampering

| Threat                                                                              | Control                                                                                                  | Reference                          |
| ----------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------- | ---------------------------------- |
| Agent writes to a file outside the workspace.                                      | Filesystem permissions restrict ops to the project directory (security.py allowlist).                    | `core/security.py`                  |
| Agent injects shell commands not in the dynamic allowlist.                          | Bash hook validates every command against the project-stack allowlist; denies unknown commands.          | Epic #26 P0 + #35 #37 gVisor opt-in |
| Agent overwrites system Python with a backdoored package.                           | Read-only rootfs; `/usr/lib/python*` not writable; pip operations confined to project venv.              | Pod spec + security profile         |

### R — Repudiation

| Threat                                                                  | Control                                                                                              | Reference                          |
| ----------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------- | ---------------------------------- |
| Agent action goes unrecorded.                                          | LiteLLM hook (Epic #35 #38) writes audit-hooks row per LLM call; agent-spawned commands logged via structlog. | `audit_hooks` + structlog      |

### I — Information disclosure

| Threat                                                                                      | Control                                                                                                                                                 | Reference                                |
| ------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------- |
| Agent reads another tenant's workspace.                                                     | Tenant Isolation Mode: per-org K8s namespace, per-org S3 prefix, default-deny NetworkPolicy.                                                            | Epic #35 #36                              |
| Agent leaks PII to the LLM provider in the prompt body.                                     | v1.2 #210 `LITELLM_AUDIT_SCRUB_OUTBOUND=true` scrubs PII via Luhn-validated regex before egress. v1.2 #207 extends scrub to Claude SDK calls.           | Epic #35 #38 + v1.2 #210 / #207          |
| Agent makes an outbound HTTP call to attacker-controlled host.                              | FQDN egress allowlist (Calico FQDN beta or Cilium CiliumNetworkPolicy) restricts to operator-allowlisted hosts.                                          | Epic #35 #36                              |
| Agent reads cluster service-account token from `/var/run/secrets/kubernetes.io/serviceaccount/`. | Per-tenant ServiceAccount with namespace-scoped RBAC; cannot read other namespaces or cluster scope.                                                 | Epic #35 #36                              |

### D — Denial of service

| Threat                                                                            | Control                                                                                       | Reference                          |
| --------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------- | ---------------------------------- |
| Agent loop consumes infinite tokens.                                              | LiteLLM enforces per-org token budget cap; budget-exceeded → fast-fail.                       | Epic #35 #38                       |
| Agent fork-bombs the pod.                                                         | Pod-level CPU + memory limits; gVisor opt-in additionally bounds syscall surface.             | Pod spec + Epic #35 #37            |
| Agent fills the workspace disk.                                                   | `emptyDir` size-limit; S3 backend has bucket quotas.                                          | Pod spec + Epic #35 #40            |

### E — Elevation of privilege

| Threat                                                                              | Control                                                                                                     | Reference                          |
| ----------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------- | ---------------------------------- |
| Agent escapes the container.                                                       | gVisor runtime (opt-in Epic #35 #37) isolates syscalls; runC default profile dropped CAP_SYS_ADMIN etc.     | `runtimeClassName: gvisor`         |
| Agent reads cluster-wide secrets.                                                  | Per-tenant ServiceAccount limited to its own namespace; OPA Gatekeeper sample policies enforce namespace shape `aifactory-tenant-*`. | Epic #35 #36 |
| Agent invokes a MCP server with elevated scope.                                    | MCP-stdio keys (Epic #35 #154) enforce per-developer scope; cannot escalate via tool args.                  | Epic #35 #154                      |

---

## LiteLLM gateway

Optional gateway for non-Claude LLM providers (opt-in via `litellm.enabled=true`).

### S — Spoofing

| Threat                                                                  | Control                                                                                  | Reference                          |
| ----------------------------------------------------------------------- | ---------------------------------------------------------------------------------------- | ---------------------------------- |
| Caller forges a tenant identity to bypass per-org budget.              | LiteLLM virtual key tied to `org_id`; key issuance audit-logged.                         | Epic #35 #38                       |
| Provider responds pretending to be a different upstream.               | TLS certificate pinning to the configured `LITELLM_API_BASE`.                            | httpx config                       |

### T — Tampering

| Threat                                                                  | Control                                                                                  | Reference                          |
| ----------------------------------------------------------------------- | ---------------------------------------------------------------------------------------- | ---------------------------------- |
| Operator changes per-org model allowlist without audit trail.          | Allowlist changes emit `org.litellm.allowlist.change` audit rows.                        | Epic #35 #38 PR-2b                 |
| Caller mutates the prompt-redaction config to bypass scrubbing.        | Config loaded server-side; not request-influenced.                                       | `litellm.conf`                     |

### R — Repudiation

| Threat                                                                 | Control                                                                                  | Reference                          |
| ---------------------------------------------------------------------- | ---------------------------------------------------------------------------------------- | ---------------------------------- |
| Tenant denies having burned a token budget.                            | `audit_hooks` row per call records org, user, model, tokens, cost; KMS-wrapped.          | Epic #35 #38                       |

### I — Information disclosure

| Threat                                                                                          | Control                                                                                                | Reference                          |
| ----------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------ | ---------------------------------- |
| LLM provider sees raw PII in the prompt.                                                        | v1.2 #210 `LITELLM_AUDIT_SCRUB_OUTBOUND=true` scrubs PII before egress (opt-in).                       | v1.2 #210                          |
| Audit row stores raw PII forever.                                                               | PII redactor (Epic #35 #38) replaces SSN/email/phone/CC patterns with placeholders before write.       | Epic #35 #38                       |
| Cross-org audit-hook reads.                                                                     | `audit_hooks` queries are `org_id`-scoped.                                                             | `services/audit_hooks_service.py` |

### D — Denial of service

| Threat                                                            | Control                                                                                  | Reference                          |
| ----------------------------------------------------------------- | ---------------------------------------------------------------------------------------- | ---------------------------------- |
| Single tenant exhausts the gateway's connection pool.            | Per-tenant rate-limit + concurrency cap.                                                 | Epic #35 #38                       |

### E — Elevation of privilege

| Threat                                                            | Control                                                                                  | Reference                          |
| ----------------------------------------------------------------- | ---------------------------------------------------------------------------------------- | ---------------------------------- |
| Caller uses an admin virtual key to bypass per-org budget.        | Admin keys cannot be issued to non-admin org members.                                    | Epic #35 #38                       |

---

## Audit subsystem

`audit_logs` table + hash chain (Epic #26 P5.2) + daily HMAC anchor (Epic #35 #43).

### S — Spoofing

| Threat                                                                              | Control                                                                                  | Reference                          |
| ----------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------- | ---------------------------------- |
| Attacker writes audit row claiming to be another user.                              | `user_id` derived from authenticated JWT; never trusted from request body.               | `services/audit_service.py`        |

### T — Tampering

| Threat                                                                                          | Control                                                                                                                                                                 | Reference                                |
| ----------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------- |
| DB admin rewrites a row to remove an incriminating action.                                      | `prev_hash` chain breaks; `verify_chain` detects on next run.                                                                                                            | Epic #26 P5.2                            |
| DB admin rewrites a row AND re-computes the chain forward.                                      | Daily HMAC anchor signed with KMS-wrapped key; re-computed chain head no longer matches the anchored value unless attacker also has KMS access AND historic anchors.    | Epic #35 #43                              |
| DB admin AND KMS-key-holder collude to rewrite history.                                         | v1.2 #208 external publication (S3 WORM / RFC 3161 / Rekor) — operator chooses publication target; AIFactory cannot mitigate without that v1.2 feature.                | Documented limitation (see below)         |

### R — Repudiation

| Threat                                                                | Control                                                                                  | Reference                          |
| --------------------------------------------------------------------- | ---------------------------------------------------------------------------------------- | ---------------------------------- |
| User denies an action.                                                | Audit row with `user_id` + signed anchor over the row's hash.                            | Epic #35 #43                       |

### I — Information disclosure

| Threat                                                            | Control                                                                                  | Reference                          |
| ----------------------------------------------------------------- | ---------------------------------------------------------------------------------------- | ---------------------------------- |
| Audit export reveals PII in `details_json`.                       | Export endpoint accepts `?max_classification=` to scope reviewer-only access.            | `routes/audit.py`                  |

### D — Denial of service

| Threat                                                            | Control                                                                                  | Reference                          |
| ----------------------------------------------------------------- | ---------------------------------------------------------------------------------------- | ---------------------------------- |
| Attacker floods audit writes to fill disk.                       | Audit writes inherit the request's rate-limit; daily retention job (`audit_retention.py`) prunes old rows. | Epic #26 P5.4 |

### E — Elevation of privilege

| Threat                                                            | Control                                                                                  | Reference                          |
| ----------------------------------------------------------------- | ---------------------------------------------------------------------------------------- | ---------------------------------- |
| Attacker disables audit writes.                                  | Audit writes happen in the same transaction as the mutated state; rollback applies if audit insert fails (fail-closed). | Per-route   |

---

## Tenant namespace

Per-organization Kubernetes namespace provisioned under Tenant Isolation Mode (Epic #35 #36).

### S — Spoofing

| Threat                                                                              | Control                                                                                  | Reference                          |
| ----------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------- | ---------------------------------- |
| Pod claims to belong to a different tenant.                                         | Per-tenant ServiceAccount; pod admission rejects mismatched SA via OPA Gatekeeper rule.  | Epic #35 #36                       |

### T — Tampering

| Threat                                                                              | Control                                                                                  | Reference                          |
| ----------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------- | ---------------------------------- |
| Attacker modifies the namespace's NetworkPolicy.                                    | NetworkPolicy is managed by AIFactory's reconciler; Argo/Flux detects drift.             | Epic #35 #36 + operator GitOps     |

### R — Repudiation

| Threat                                                                | Control                                                                                  | Reference                          |
| --------------------------------------------------------------------- | ---------------------------------------------------------------------------------------- | ---------------------------------- |
| Operator denies provisioning a tenant.                                | `tenant_states` row with `created_at`, `reconcile_at` timestamps; audit row per reconcile. | Epic #35 #36                      |

### I — Information disclosure

| Threat                                                                                  | Control                                                                                  | Reference                          |
| --------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------- | ---------------------------------- |
| Pod in tenant A reads secrets from tenant B's namespace.                                | Per-tenant ServiceAccount limited to its own namespace by RBAC; default-deny NetworkPolicy. | Epic #35 #36                    |
| DNS leakage reveals tenant existence via `<tenant>.svc.cluster.local`.                  | Egress allowlist forbids tenant-to-tenant DNS resolution at the FQDN policy layer.       | Epic #35 #36                       |

### D — Denial of service

| Threat                                                            | Control                                                                                  | Reference                          |
| ----------------------------------------------------------------- | ---------------------------------------------------------------------------------------- | ---------------------------------- |
| One tenant consumes all node CPU/memory.                          | Per-namespace ResourceQuota; per-pod LimitRange.                                         | Epic #35 #36                       |

### E — Elevation of privilege

| Threat                                                            | Control                                                                                  | Reference                          |
| ----------------------------------------------------------------- | ---------------------------------------------------------------------------------------- | ---------------------------------- |
| Reconciler's Vault AppRole reads tenant secrets.                  | Reconciler AppRole has `sys/policies/acl/aifactory-tenant-*` MANAGE permissions but NO `data/aifactory/orgs/*` READ.  | Epic #35 #36 + concept doc        |
| Pod uses node-level credentials (instance metadata).              | gVisor opt-in blocks the metadata endpoint; IRSA / Workload Identity provides scoped credentials per ServiceAccount.   | Epic #35 #36 + #37                 |

---

## Documented limitations

Limitations in v1.1 that an attacker could exploit but AIFactory does not currently mitigate. Operators MUST be aware:

1. **Collusion of DB admin + audit-key holder (Tampering — Audit subsystem).** If the same individual has Postgres admin AND access to the KMS-wrapped audit-signing key, they can rewrite the audit chain and the daily anchor. **Mitigation**: keep KMS-key access in a separate role from DB admin. **Closure**: v1.2 #208 ships external anchor publication (S3 WORM / RFC 3161 / Sigstore Rekor) — once enabled, even the colluding pair cannot rewrite the externally-published anchors retroactively.
2. **JWT stateless window (Elevation — Web pod).** A JWT remains valid until its TTL even after the user is disabled at the IdP. Default 15 min; operator can shorten via `JWT_ACCESS_TTL_SECONDS`. **Closure**: v1.2 #209 SAML SLO propagates IdP-side disable to AIFactory immediately on logout, but the residual stateless window remains for any JWT issued before the SLO event.
3. **Claude SDK calls bypass LiteLLM enforcement in v1.1 (Information disclosure — Agent pod).** Claude calls in v1.1 are NOT routed through LiteLLM; per-tenant budget cap + model allowlist + scrubBeforeSend do not apply to Claude. **Closure**: v1.2 #207 wraps Claude SDK calls in an enforcement shim.
4. **gVisor opt-in (Elevation — Agent pod).** gVisor is opt-in via `runtimeClassName: gvisor`. Operators who do not opt in run agents on the default runC runtime; container-escape CVEs in the kernel become exploitable. **Mitigation**: enable gVisor in production; gate via OPA Gatekeeper sample policy.
5. **Calico FQDN policy is beta (Information disclosure — Agent pod).** The FQDN-based egress allowlist depends on Calico's FQDN policy (currently beta) or Cilium's `CiliumNetworkPolicy`. Operators on an unsupported CNI cannot enforce FQDN egress. **Mitigation**: Helm pre-install hook hard-fails when neither CRD is present, so operators discover the gap at install time.
6. **No client-side IDS for prompt-injection attacks (Information disclosure — Agent pod).** A malicious external document loaded into a prompt could attempt prompt-injection to exfiltrate. **Mitigation**: operator-installed prompt-classification / policy layer (e.g. Lakera Guard, NeMo Guardrails) in front of LiteLLM. AIFactory does not ship this in v1.1.
7. **No protection against compromised LLM provider (Tampering — LiteLLM gateway).** If the LLM provider returns a malicious tool-call output, AIFactory's strict schema validation rejects unknown tool calls but cannot prevent the provider from refusing service or returning subtly-biased outputs. **Mitigation**: vendor diligence + DPA review; AIFactory's multi-provider abstraction lets operators switch providers fast.

---

## Update cadence

- **Quarterly**: walk through every component table; mark each control as verified for the current audit window.
- **After each major architectural change**: add or update the affected table in the same PR.
- **After each public CVE in a dependency**: update the relevant `Reference` cell + retag the threat in the right STRIDE row if the attack-path enumeration changes.

```bash
git log --oneline guides/security/threat-model.md
```

Pair with the DPIA [`../compliance/dpia-data-flow.md`](../compliance/dpia-data-flow.md) — the threat model tells you what can go wrong; the DPIA tells you what kind of data is exposed when it does.
