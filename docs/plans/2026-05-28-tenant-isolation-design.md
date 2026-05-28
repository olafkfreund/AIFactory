# Design — Tenant Isolation Mode (Epic #35 #36)

> Locked from super-brainstorm 2026-05-28. Reviewer-style audit pass
> next; implementation in 3 PRs after sign-off.

## Why we're doing this

The v1.0 model assumes every Organization in a deployment trusts every
other Organization in that deployment — they share the same Kubernetes
namespace, the same S3 bucket, the same Vault path, the same agent
pod-spawner with cluster-wide RBAC. That's fine for "one bank, one
deployment, internal teams" but **fails** for "one bank, one deployment,
hostile internal teams" (e.g. trading vs M&A under Chinese Walls) or
"one MSP, one deployment, many client orgs."

#36 ships Tenant Isolation Mode: opt-in per-deployment toggle that
reconciles each Organization into its own Kubernetes namespace with
per-tenant ServiceAccount, NetworkPolicy, S3 prefix + IAM, and Vault
path. Agent pods spawn into the tenant's namespace and inherit the
isolation by construction.

## Out of scope (explicit)

- **Cross-cluster isolation.** v1.1 assumes one cluster per AIFactory
  deployment. Operators wanting per-tenant clusters use multiple
  Helm installs.
- **Per-tenant LLM provider.** All tenants share the deployment's
  configured LLM provider (LiteLLM per-tenant routing lands in #38).
- **Per-tenant Postgres database.** All tenants share the same DB
  schema with row-level `org_id` filtering. Per-tenant DBs are
  v2.0 (separate epic).
- **OpenShift-specific SCCs.** Standard K8s NetworkPolicy + RBAC
  only; OpenShift overlay is parking-lot per epic #35.
- **Kata Containers / Firecracker.** gVisor only (epic #35 #37
  already shipped). Higher-isolation runtimes deferred.
- **Tenant-scoped audit-chain anchor.** v1.1 has one chain across
  all tenants (Epic #35 #43). Per-tenant chain + per-tenant anchor
  is v1.2.

## Locked decisions

### 1. Reconciler home — In-app FastAPI background task (with leader election)

The reconciler runs as an `asyncio` task inside the web-server's
lifespan. Triggered on `Organization` create/delete plus a periodic
resync every 5 minutes (catches missed events + drift).

```
apps/web-server/server/services/tenant_reconciler.py
  ├─ start_reconciler_loop()  — lifespan hook
  ├─ reconcile_org(org)        — single org pass
  └─ reconcile_all()           — periodic sweep
```

**Failure-safe contract:** every reconcile step wrapped in `try/except`.
A stuck or failing reconcile logs WARNING + retries next tick. A
broken reconciler does NOT crash the web pod (matches the pattern
from #41/#42/#43).

**Leader election (reviewer finding #2):** v1.1 multi-replica
deployments via Redis pub/sub (#40) mean N replicas = N reconcilers
without a coordination mechanism. K8s/IAM/Vault writes are not
all idempotent (Vault `CreateRole` returns 400 on duplicates unless
PUT; IAM `CreateRole` returns `EntityAlreadyExists`; the 409 from
K8s is OK but the cascade through to Vault/IAM is not). 

Solution: distributed mutex via Redis. Before any K8s/IAM/Vault
write, the reconciler acquires `SETNX aifactory:tenant-recon-lock:<org-id>
EX 60`. The replica that wins acquires; others skip. On
completion, the lock is released. If Redis is unavailable
(`redis.enabled=false`), the reconciler logs WARNING + refuses to
write — single-replica is the only safe mode without leader
election.

**Why not a separate Operator?** Kubernetes-idiomatic Operator
(Kopf-based controller watching `AIFactoryOrg` CRDs) would be ~1 week
extra code + a second Helm chart + a second image to maintain. The
in-app reconciler reuses the existing DB session + Helm Secret access
and ships in less than half the effort. We migrate to a separate
Operator in v2.x if/when the reconciler outgrows the web pod.

**Required RBAC for the web pod's ServiceAccount (reviewer finding #4):**
- `create`/`update`/`delete`/`get`/`list` on `Namespace`,
  `ResourceQuota`, `LimitRange`, `ServiceAccount`, `Role`,
  `RoleBinding`, `NetworkPolicy`, `ExternalSecret`.
- **Honest acknowledgement: K8s RBAC does NOT support prefix-based
  namespace scoping at the RoleBinding level.** A web pod with
  cluster-wide `create Namespace` + `create RoleBinding` is
  effectively cluster-admin. Two options:
  1. **v1.1 acceptance:** accept that the web pod has high privilege;
     mitigate via gVisor (#37) on the web pod, container-level
     hardening, and operator awareness ("the reconciler RBAC is a
     known privilege concentration").
  2. **OPA Gatekeeper / Kyverno policy** (recommended for production
     deployments): the operator installs a `ConstraintTemplate` that
     denies any Namespace create whose name doesn't match
     `aifactory-tenant-*`, and any RoleBinding whose subjects
     reference the web pod's SA in a non-tenant namespace.
     Sample policies ship in `charts/aifactory/templates/gatekeeper/`
     (Helm-conditional on `tenant.gatekeeperEnabled`).
- The threat model below has been updated: "Web pod compromise →
  cluster-wide breakout" is **partially defended** in v1.1 (via
  gVisor + OPA when enabled), **undefended** when OPA is not installed.

### 2. Namespace naming — `aifactory-tenant-<org-slug>`

```
aifactory-tenant-acme        (Organization.slug = "acme")
aifactory-tenant-corp-eu     (Organization.slug = "corp-eu")
```

**Immutability:** the namespace name is computed once on org creation
and stored in a new `organizations.tenant_namespace` column. Subsequent
`Organization.slug` renames do NOT change the namespace name — the
reconciler keeps writing to the original. Documented operator
expectation: if a slug rename matters operationally, recreate the org.

The `Organization.slug` column is already URL-safe + unique
(`String(255), unique=True, nullable=False`), so no slug-format
validation is added here.

**Slug-rename UX guard (reviewer recommendation #1):** the
`PATCH /api/orgs/{id}` endpoint emits, on every slug change where
`tenant_namespace` is set:
- An audit log entry at WARNING severity (`org.slug.rename`,
  `classification='internal'`, `details_json` records old + new
  slug and the now-stale namespace name).
- A Kubernetes Event of type `Warning`, reason `SlugRenamed`, on the
  tenant namespace (visible in `kubectl describe ns`).
- A response-body field `tenant_namespace_unchanged: true` so the
  frontend can render an inline warning.

### 3. NetworkPolicy egress — Default-deny + explicit allowlist

Each tenant namespace gets a `default-deny-egress` NetworkPolicy plus
an `allow-egress-known-destinations` policy permitting:

- **Anthropic API** (or whatever LLM provider the org has configured):
  `api.anthropic.com:443` resolved via DNS to a CIDR or per-FQDN
  egress rule (depends on the cluster's CNI plugin; both Calico and
  Cilium support FQDN policies).
- **Operator-configured OIDC/SAML IdP** host(s).
- **KMS backend endpoint** (AWS KMS regional endpoint / Vault address
  / Azure KV / GCP KMS — read from `KMS_BACKEND` config at reconcile
  time).
- **kube-dns** (`kube-system/kube-dns:53` UDP+TCP).

Everything else — including arbitrary HTTP, S3 (which the tenant
accesses via the S3 IRSA path, NOT direct HTTP), cross-tenant
namespaces, and external attacker-controlled hosts — is blocked.

**Ingress:** default-deny ingress; allow only the AIFactory web pod
(which sits in the deployment-default namespace) to talk to agent
pods in tenant namespaces.

**CNI capability (reviewer finding #6):** FQDN-based egress policies
require Calico (FQDN beta, performance issues at scale) or Cilium
(via `CiliumNetworkPolicy` CRD). kube-router, weave-net, and stock
flannel do NOT support FQDN. The previous draft said "the worst case
is the agent can't reach api.anthropic.com — operationally noisy" —
that framing is misleading. EVERY agent task for EVERY tenant
fails on a non-FQDN-supporting CNI. Locked: **Helm `pre-install`
hook probes for `crd.projectcalico.org` or `cilium.io` CRDs and
hard-fails the install** when `tenant.isolationEnabled=true` and
neither is present. Operators see the error at `helm install` time,
not at first reconcile. The `tenant.networkPolicy.cniBackend` value
(`calico` | `cilium` | `auto`) selects which CRD shape the
reconciler emits; `auto` tries both.

### 4. S3 isolation — Per-tenant IAM role + per-tenant prefix

One bucket (operator-supplied via `workspaces.storage.uriBase`). Each
tenant gets a prefix:

```
s3://aifactory-prod/orgs/<org-uuid>/workspaces/
s3://aifactory-prod/orgs/<org-uuid>/anchors/    (future: per-tenant anchors)
```

**IAM model (reviewer finding #1 — corrected from earlier draft):**
the previous draft used `${aws:PrincipalTag/org-uuid}` interpolation,
which requires session tags injected at AssumeRole time + a mutating
admission webhook to add them to the IRSA token. That's complex +
unreliable. Locked instead: **one IAM role per tenant, hard-coded
prefix in the role's policy.**

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
    "Resource": "arn:aws:s3:::aifactory-prod/orgs/<org-uuid>/*"
  }, {
    "Effect": "Allow",
    "Action": "s3:ListBucket",
    "Resource": "arn:aws:s3:::aifactory-prod",
    "Condition": {"StringLike": {"s3:prefix": "orgs/<org-uuid>/*"}}
  }]
}
```

The reconciler creates one IAM role per tenant; the tenant's
ServiceAccount is annotated with `eks.amazonaws.com/role-arn=<arn>`;
IRSA's standard OIDC trust policy lets that SA assume that specific
role. Cross-tenant access fails with `AccessDenied` at the AWS API
level — even a misconfigured web pod cannot bypass.

**Operational trade-off:** one IAM role per tenant means N orgs = N
IAM roles. AWS soft-limit is 5,000 roles per account; operators with
>500 orgs should request a limit increase or migrate to the session-
tag approach (future work).

**For non-AWS clusters (GKE Workload Identity / AKS AAD Pod Identity /
on-prem with static credentials):** the equivalent isolation story is
operator-supplied per-tenant credentials. Documented as operator
responsibility in the concept doc with worked examples for each
platform.

### 4a. S3 recursive delete safety (reviewer recommendation #4)

Stage-2 tear-down's `s3 rm` MUST refuse to delete any prefix that
doesn't match `^orgs/[0-9a-f-]{36}/$`. A 24-hour dry-run log pass
SHOULD precede the actual deletion (operator can intervene during
the window). Both are enforced in `tenant_reconciler.py`'s
`_tear_down_s3()` function.

### 5. Vault layout — Single mount `aifactory/` with per-tenant path

```
aifactory/
├── shared/                  ← deployment-wide secrets (KMS key wraps, etc.)
├── orgs/
│   ├── <org-uuid-1>/
│   │   ├── oidc-client-secret
│   │   ├── saml-sp-cert
│   │   └── ...
│   └── <org-uuid-2>/
│       └── ...
```

Each tenant's ServiceAccount gets a Vault policy:

```hcl
path "aifactory/data/orgs/<org-uuid>/*" {
  capabilities = ["read", "list"]
}
```

The reconciler creates the policy + binds it to the tenant's Vault
Auth role (Kubernetes auth method's role for the SA).

**Reconciler's Vault identity (reviewer finding #3):** the web pod
authenticates to Vault via the **Kubernetes auth method** using its
own ServiceAccount token. The operator pre-creates a dedicated
AppRole policy `aifactory-reconciler` with the minimum-needed
capabilities:

```hcl
# Create/update tenant policies + roles only.
path "sys/policies/acl/aifactory-tenant-*" {
  capabilities = ["create", "update", "delete", "read"]
}
path "auth/kubernetes/role/aifactory-tenant-*" {
  capabilities = ["create", "update", "delete", "read"]
}
# Cannot read tenant secrets themselves — only manage the
# policies that grant access.
```

The Helm chart includes a documented pre-install step + a sample
Terraform module to bootstrap this AppRole. **Never** use a root
token for the reconciler — documented as a forbidden anti-pattern
in the concept doc.

**Operator workflow:** to add a tenant-scoped secret, `vault kv put
aifactory/orgs/<org-uuid>/whatever value=...`. Existing `vault kv
metadata` works unchanged for backup/restore.

### 6. Agent scheduling — Spawner reads `tenant_states` for namespace

A new `tenant_states` table tracks the reconciled state per org:

```sql
CREATE TABLE tenant_states (
    org_id              VARCHAR(36) PRIMARY KEY REFERENCES organizations(id) ON DELETE CASCADE,
    isolation_mode      VARCHAR(16) NOT NULL DEFAULT 'shared',  -- 'shared' | 'isolated'
    namespace_name      VARCHAR(63),     -- the immutable namespace name
    service_account     VARCHAR(63),     -- per-tenant SA name in that namespace
    iam_role_arn        VARCHAR(2048),   -- IRSA role ARN (AWS) or empty
    vault_policy_name   VARCHAR(255),    -- e.g. "aifactory-tenant-<org-uuid>"
    reconciled_at       TIMESTAMP,       -- last successful reconcile
    reconcile_error     TEXT,            -- non-null when last reconcile failed
    created_at          TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMP NOT NULL DEFAULT NOW()
);
```

The existing `agent_service` spawner takes a `task_id` → `org_id` →
queries `tenant_states` for `namespace_name` + `service_account`. The
agent pod is spawned into that namespace with that SA.

**Backward compat:** orgs without a `tenant_states` row (or with
`isolation_mode='shared'`) fall back to the deployment-default
namespace. Pre-#36 deployments are byte-for-byte unchanged until the
operator flips `tenant.isolationEnabled=true`.

### 7. Tear-down lifecycle — Soft-delete + 30-day grace period

Org delete is a two-stage flow:

**Stage 1 — Soft delete (immediate):**
- Set `Organization.deleted_at` (new column).
- Scrub PII per GDPR (P5.5): null email/name on all User rows whose
  membership is exclusively to this org, hash user_id in audit_logs.
- Reconciler marks `tenant_states.isolation_mode='deleted'` (new
  enum value); the agent spawner refuses new tasks for this org.
- Existing agent pods continue running until completion; no new
  pods can be created.

**Stage 2 — Tear-down (day 30):**
- Reconciler deletes the Kubernetes Namespace (cascades to all child
  resources via the deletion timestamp).
- Deletes the S3 prefix (recursive `s3 rm`, with §4a's prefix-shape
  assertion + 24-hour dry-run pass).
- Deletes the Vault path + policy.
- Removes the `tenant_states` row.
- Audit-logs the tear-down with `classification='confidential'`.

**Stuck-terminating namespace handling (reviewer finding #5):**
Kubernetes Namespace deletion can stall indefinitely on stuck
finalizers (ExternalSecret CRs with stale ESO finalizers, custom CRDs
with unavailable controllers, etc.). The reconciler detects:

```python
if (
    ns.status.phase == "Terminating"
    and (now - ns.metadata.deletionTimestamp) > timedelta(minutes=30)
):
    # Stuck. Emit operator alert (audit log + WARNING).
    # Optionally force-remove known-safe finalizers on ESO CRs.
    # Do NOT force-remove the Namespace finalizer itself — that's
    # a destructive operation that should require operator decision.
```

The audit log + WARNING fires every reconcile tick until the
operator intervenes. The `tenant_states.reconcile_error` column
records the stuck-terminating state for SQL-queryable health checks.

**GDPR Article 17 distinction (reviewer recommendation #3):** the
30-day grace period applies to INFRASTRUCTURE resources only
(namespace, S3 prefix, Vault path). PII (`User.email`, `User.name`,
hashed `user_id` in audit logs) is scrubbed IMMEDIATELY on
soft-delete in stage-1. This satisfies GDPR Art. 17's "without undue
delay" for personal data while the infrastructure grace period
covers operational recovery (mistaken-delete, legal-hold needs).
The CHANGELOG + concept doc state this explicitly so an ISO 27001
auditor sees the distinction.

Operators wanting different grace periods set
`tenant.deletionGraceDays` (default 30). Day-0 (immediate) is allowed
but produces a WARNING log on every reconcile pass reminding the
operator of the recovery-window loss.

### 8. Opt-in scope — Deployment-wide toggle

`tenant.isolationEnabled` in `values.yaml`:

```yaml
tenant:
  isolationEnabled: false   # default — shared-namespace mode (legacy)
  deletionGraceDays: 30
  namespacePrefix: "aifactory-tenant"  # operator override if needed
```

When `false`: reconciler doesn't run; all orgs map to the deployment-
default namespace. Byte-for-byte unchanged from pre-#36 deployments.

When `true`: reconciler runs; every org (existing + new) gets its
isolated tenant. Existing orgs get a migration: on first reconcile,
the reconciler creates the namespace + resources, then the agent
spawner switches the org's next-task spawn into the new namespace.
**No mid-task migration** — in-flight tasks finish in the shared
namespace.

**Why not per-org?** Mixed-tenant ("free-tier shares, paid-tier
isolated") is a SaaS pattern. AIFactory's v1.1 self-hosted model
typically means all orgs are equally trusted OR all are equally
untrusted. Adding per-org granularity = new column + migration + UI;
operators wanting it can wait for v2.0.

## Failure-safe contract

Same as #40/#41/#42/#43:
- Every reconcile step wraps in `try/except`. Failures are logged at
  WARNING + retry on next tick.
- A broken reconciler does NOT crash the web pod. The lifespan task
  catches all exceptions; structured log + sleep + retry.
- `tenant_states.reconcile_error` records the last failure for
  operator visibility (`SELECT org_id, reconcile_error FROM
  tenant_states WHERE reconcile_error IS NOT NULL`).
- If the cluster's CNI plugin doesn't support FQDN NetworkPolicy
  (rare; both Calico + Cilium do), the reconciler skips the FQDN-
  specific policies + logs a WARNING. The default-deny still applies,
  so the worst case is "the agent can't reach api.anthropic.com" —
  operationally noisy, not a security regression.

## Threat model

| Threat | Pre-#36 | Post-#36 (isolated) |
|--------|---------|---------------------|
| Tenant A's web client reads tenant B's audit log | Defended by `org_id` filter | Defended (same — DB-level) |
| Tenant A's prompt exfils data to attacker-controlled URL | **Undefended** (agent egress wide open) | Defended (NetPol default-deny) |
| Tenant A's agent reads tenant B's workspace files in S3 | **Partially undefended** (relies on application-level filter) | Defended (IAM `s3:prefix`) |
| Tenant A's agent reads tenant B's Vault secrets | **Partially undefended** (relies on application-level filter) | Defended (Vault policy) |
| Compromised tenant A pod tries to schedule into tenant B's namespace | N/A (one namespace) | Defended (per-tenant SA has no cross-namespace RBAC) |
| Web pod compromise → cluster-wide breakout | Mitigated by web pod SA's limited RBAC | Same (web pod RBAC unchanged) |
| Reconciler bug creates wrong tenant's resources | N/A | Detected via `reconcile_error` log + audit |

## Implementation plan — 3 PRs

### PR-1 — Schema + reconciler core

- Alembic migration: `tenant_states` table + `Organization.tenant_namespace`
  + `Organization.deleted_at` columns.
- ORM models: `TenantState`.
- `apps/web-server/server/services/tenant_reconciler.py` — reconciler
  loop with the failure-safe pattern; no Kubernetes API calls yet
  (logs the actions it would take). This lets the rest of the PR
  series build against a real reconciler interface.
- Unit tests for the reconciler decision logic.

### PR-2 — Kubernetes resources

- `KubernetesClient` wrapper (lazy `kubernetes-asyncio` SDK).
- Reconciler creates Namespace + ServiceAccount + Role + RoleBinding
  + NetworkPolicy + ResourceQuota + LimitRange.
- S3 IAM policy attached to the SA's IRSA role (when AWS); operator
  doc explains the equivalent for non-AWS.
- Vault policy + role binding via `hvac`.
- Agent spawner updated to read `tenant_states` for the target
  namespace.
- Tests with a minikube fixture in the `helm` CI job.

### PR-3 — Helm + tear-down + concept doc

- `tenant:` block in `values.yaml` with `isolationEnabled`,
  `deletionGraceDays`, `namespacePrefix`.
- Helm chart: web pod's ClusterRole + per-prefix RoleBinding so it
  can manage tenant namespaces.
- Tear-down stage-2 job (a small daily cron).
- `docs/docs/concepts/tenant-isolation.md` user-facing concept page.
- `guides/compliance/iso27001-evidence.md` update — A.13.1 network
  segmentation now ✅ for isolated deployments.

## Open questions (to be resolved at review time)

- **Per-namespace `LimitRange`**: should the reconciler set per-pod
  CPU/memory limits in tenant namespaces, or leave that to the
  operator's cluster-level defaults? Recommend: yes, with operator-
  tunable values; prevents noisy-neighbor between tenants.
- **`ResourceQuota`**: should the reconciler cap pod count / PVC count
  per tenant? Recommend: yes, with operator-tunable values; prevents
  one tenant's runaway from exhausting cluster quota.
- **Per-tenant audit-chain anchor** (relation to #43): not in v1.1
  scope. Documented in the audit-anchor concept doc; revisit in v1.2.

## Decision audit summary

8 of 8 brainstorm decisions taken on recommended options. Reviewer
audit pass added 6 critical findings + 5 recommendations, all baked
in above:

| Finding | Resolution |
|---------|------------|
| IRSA `PrincipalTag` is wrong condition key | Locked: one IAM role per tenant with hard-coded prefix (§4); future session-tag work for >500 orgs |
| No leader election → multi-replica reconciler race | Redis SETNX mutex per org-id; refuse to write when Redis unavailable (§1) |
| Vault privileged-access mechanism unspecified | Locked: dedicated `aifactory-reconciler` AppRole with minimum-needed `sys/policies/acl/aifactory-tenant-*` + `auth/kubernetes/role/aifactory-tenant-*` capabilities; root token explicitly forbidden (§5) |
| K8s RBAC doesn't support prefix matching | Honestly acknowledged: web pod has high privilege; gVisor mitigates; OPA Gatekeeper / Kyverno policy sample ships in chart for production. Threat model updated (§1) |
| Stuck-terminating namespace has no recovery | Locked: 30-min detection → audit log + WARNING + `tenant_states.reconcile_error`; force-finalizer-removal NOT automated (§7 stage-2) |
| CNI FQDN-policy fallback silently broken | Locked: Helm pre-install hook hard-fails when neither Calico nor Cilium CRDs present (§3) |
| Slug-rename UX gap | Audit log + K8s Event + frontend warning (§2) |
| S3 recursive delete safety | Prefix-shape assertion + 24h dry-run pass (§4a) |
| In-flight task during isolation flip | Documented as user-visible error; no mid-task migration; test matrix in PR-2 (§8) |
| GDPR Art. 17 vs 30-day grace | Documented distinction: PII immediate, infra grace (§7 stage-2) |
| LimitRange / ResourceQuota deferred | TBD — answered in PR-2's first commit with operator-tunable defaults |

No deviations from brainstorm intent — refinements tighten the
design without changing scope.
