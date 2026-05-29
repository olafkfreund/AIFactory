# Design — Per-tenant audit-chain anchor (Epic #35 v1.2 #208)

> Locked from review of #43 (audit anchor) + #36 (tenant isolation)
> deferrals on 2026-05-29. Implementation in 3 PRs after sign-off.
>
> Closes the deferral noted in
> [docs/plans/2026-05-28-audit-anchor-design.md](2026-05-28-audit-anchor-design.md)
> ("per-tenant chain + per-tenant anchor is v1.2") and in
> [docs/plans/2026-05-28-tenant-isolation-design.md](2026-05-28-tenant-isolation-design.md)
> §"Open questions" ("Per-tenant audit-chain anchor (relation to #43):
> not in v1.1 scope. Documented in the audit-anchor concept doc;
> revisit in v1.2").

## Why we're doing this

v1.1's audit-chain anchor (Epic #35 #43) ships **one** signed chain
across **all** tenants in a deployment. Every audit row in
`audit_logs` participates in the same per-row `prev_hash` chain, and
the daily cron emits **one** `audit_anchors` row covering the chain
head of every tenant's rows combined.

That's fine for "one bank, one deployment, internal teams" — the
operator and the compliance officer share the same trust boundary.
It **fails** the moment we ship Tenant Isolation Mode (#36) for "one
MSP, many client orgs" or "one bank, Chinese-wall-separated trading
desks." Two concrete gaps:

1. **A tenant cannot independently verify their own audit log.**
   Their auditor receives an NDJSON export of the tenant's rows plus
   the deployment-wide anchors. The anchors sign a chain head
   computed over **everyone's** rows; the tenant's auditor can't
   re-compute it without seeing rows that belong to other tenants.
   The auditor's only option is to trust the operator's
   cross-tenant chain — which collapses the whole point of an
   externally-verifiable anchor.

2. **A cross-tenant chain leaks tenant existence.** Even the v1.1
   filtered export (`?org_id=<uuid>`) loses chain-verifiability for
   the same reason the `?max_classification` filter does: removing
   rows leaves gaps in `prev_hash` continuity. The audit anchor
   service even documents this explicitly
   ([audit_export.py:188-192](../../apps/web-server/server/services/audit_export.py)):
   *"a multi-tenant export with per-org filtering loses the chain-
   verification property... the route layer rejects that combination
   explicitly."* Today the only auditor-bound export is the WHOLE
   deployment's chain, which means tenant A's auditor sees rows for
   tenants B, C, D — a tenancy violation in itself.

True multi-tenant audit independence for ISO 27001 needs **per-tenant
chains anchored by per-tenant keys**:

- Each tenant's `audit_logs` rows chain to a per-tenant chain head.
- The daily cron emits one anchor row **per tenant**, signing that
  tenant's chain head with a 32-byte HMAC key **unique to that
  tenant**, KMS-wrapped per the existing
  [audit_signing_keys](../../apps/web-server/server/database/models.py)
  store extended with `org_id`.
- The tenant's auditor receives the tenant's rows + the tenant's
  anchors + (via operator runbook) the tenant's unwrapped key. The
  auditor verifies independently without seeing or trusting any
  other tenant's data.

ISO 27001 sub-controls that benefit:

- **A.12.4.2 Protection of log information** — independent
  verification per tenant satisfies the "log information protected
  against tampering" requirement at tenant granularity (today's
  shared-chain version protects against tampering at deployment
  granularity, which doesn't meet the control when "the deployment"
  is many client tenants under one operator).
- **A.12.4.3 Administrator and operator logs** — separates the
  operator's privilege scope from the tenant's verification scope.
  An operator with DB write access + the shared HMAC key can rewrite
  any tenant's chain in v1.1; with per-tenant keys, rewriting
  tenant A requires tenant A's key, which lives in tenant A's Vault
  path (#36 §5) the operator's reconciler can write to but cannot
  read from (per the
  [aifactory-reconciler AppRole](2026-05-28-tenant-isolation-design.md)
  policy locked at #36 §5).
- **A.18.1.3 Protection of records** — gives each tenant the
  evidence they need for their own ISMS / SOC2 audit without
  exposing other tenants' records.
- **A.18.2.2 Compliance with security policies and standards** —
  the per-tenant verification path lets the tenant's auditor
  independently attest to the operator's policy compliance,
  satisfying the "independent review" expectation.

The relevant ISO 27001 concept this design enables is **separation
of duties between tenants** under a single operator. Today, the
operator has unilateral power over the shared chain; in v1.2 the
operator still hosts the infrastructure but the tenant's auditor
holds the cryptographic verification authority for the tenant's
slice.

## Out of scope (explicit)

- **Cross-tenant audit-search by the operator.** The operator may
  legitimately need to query "show me every `kms.key.unwrap` event
  across all tenants in the last 24 hours" for incident response.
  v1.2 does NOT ship that endpoint. The shared-chain export
  (existing v1.1) still serves the operator's view; per-tenant
  exports serve the tenant's view. A dedicated "support audit"
  endpoint with separate auth (operator break-glass token, every
  call audit-logged at `classification='confidential'`) lands in
  v1.3+ as its own design.

- **Migration of existing audit_logs rows to per-tenant chains.**
  Pre-#208 rows have a deployment-wide `prev_hash` that depends on
  the inter-tenant interleaving. Rewriting them into per-tenant
  chains means recomputing every `prev_hash` for every row from
  genesis to the migration cutover — a destructive one-time DB
  rewrite that breaks every existing anchor's verification.
  Locked deferral: pre-#208 rows keep the shared chain forever; the
  per-tenant chain starts at the migration cutover (the row's
  `prev_hash` for the FIRST per-tenant-chained row is `GENESIS-T-<org-uuid>`,
  a tenant-scoped sentinel). The export endpoint emits two
  segments: pre-cutover rows under the shared chain + their shared
  anchors, then post-cutover rows under the tenant chain + tenant
  anchors. The cutover boundary is recorded in `tenant_audit_state`
  (§4 decision 2) so verifiers know where to switch chain rules.
  Operators wanting a clean per-tenant chain from row 1 must
  provision a fresh deployment.

- **Per-tenant key rotation cadence customisation.** The shared
  key in #43 rotates on operator runbook trigger (no automatic
  cadence). v1.2 uses the same operator-triggered rotation for
  per-tenant keys; we do NOT add per-tenant cadence overrides
  ("tenant A wants 90-day rotation, tenant B wants 30-day"). When a
  tenant needs faster rotation, the operator runs the rotation
  runbook for that tenant. Lands as a "per-tenant rotation knob" in
  v1.3+ if operator demand exists.

- **GDPR Article 17 erasure of a tenant's chain on org delete.**
  The tenant-isolation #36 PR-3 tear-down at day 30 deletes the
  tenant's K8s namespace + S3 prefix + Vault path. The tenant's
  audit chain + anchors **stay** in the shared DB indefinitely for
  legal-hold + multi-year audit-trail requirements. The
  `audit_signing_keys` row stays so historical anchors remain
  verifiable. The `tenant_audit_state` row is marked
  `lifecycle='sealed'` (verification still possible, no new rows
  ever appended). PII inside the chained rows is already scrubbed
  per #36 stage-1 (immediate GDPR erasure). Operators with
  stronger Art-17 obligations can run a separate
  `aifactory audit purge-org` admin tool (v1.3+) which rewrites
  the affected rows' `details_json` + breaks the chain past the
  purge point; out of v1.2 scope because the destructive operation
  needs its own design.

- **Per-tenant verifier CLI distribution.** The verifier helper in
  [audit_export.py](../../apps/web-server/server/services/audit_export.py)
  (`verify_anchored_export`) gets a per-tenant-aware version
  (§5 decision 5). We do NOT ship a separately-packaged
  `aifactory-verify` PyPI / Homebrew binary in v1.2 — the
  reference verifier is `python -m server.audit verify-anchor
  --org-id <uuid>` run from the AIFactory web pod (or a sidecar
  with DB + KMS access). Standalone tooling lands in v1.3+ when
  the external-publication work (S3 WORM, Sigstore — also v1.3+)
  needs a verifier that doesn't depend on AIFactory's KMS plumbing.

- **Per-tenant external publication (S3 WORM / RFC 3161 / Sigstore).**
  Same deferral as v1.1's #43: external pub for the per-tenant
  anchors is v1.3+. The DB-as-substrate trust model from #43 §3
  carries over for v1.2 — documented explicitly in the concept doc.

## Architectural options — chosen with reasoning

Three paths considered. **Option A picked.**

### Option A (CHOSEN) — One chain per tenant + one signing key per tenant

Each tenant gets:

- A per-tenant chain: the FIRST `audit_logs` row written after the
  org is provisioned uses `prev_hash = 'GENESIS-T-<org-uuid>'`; every
  subsequent row for that org chains to the previous row **for the
  same org**, ignoring rows belonging to other tenants. The
  `audit_logs.org_id` column (already exists, line 583 of
  `models.py`) is the partition key for the chain.
- A per-tenant signing key: one row in `audit_signing_keys` per
  tenant, KMS-wrapped per the existing #43 mechanism, with the new
  `org_id` column scoping it. The key is generated by the tenant
  reconciler ([tenant_reconciler.py](../../apps/web-server/server/services/tenant_reconciler.py))
  during the first reconcile pass when `isolation_mode='isolated'`
  is set, and the wrapped key blob is also written to the tenant's
  Vault path (`aifactory/orgs/<org-uuid>/anchor-key-wrapped`, see
  §6 decision 7) so the tenant's auditor can retrieve it via the
  operator runbook without DB access.
- A per-tenant chain head, signed daily by the cron, written as one
  `audit_anchors` row per tenant per day. The cron iterates over
  every isolated tenant; the existing shared cron path keeps
  running for non-isolated tenants (backward compat).

**Strongest isolation story.** Compromise of tenant A's key
(somehow exfiltrated from the tenant's Vault path) lets the attacker
forge tenant A's anchors but not tenant B's. Compromise of the KMS
root key affects all tenants equally (acknowledged in the threat
model; same property as v1.1).

**Trade-off — KMS overhead.** N tenants × per-tenant key unwrap per
cron pass means an N-fold increase in KMS API calls vs the shared
chain. Most KMS backends rate-limit at the regional API-call level
(AWS KMS: 5,500 unwraps/sec per region for the GenerateDataKey API
family; Vault: capped by storage backend throughput; Azure KV:
2000/sec per key vault). At 10k tenants the daily cron does 10k
unwraps + 10k signs + 10k writes in a tight loop; rate-limit risk
is real but bounded. The cron's failure-safe contract (every per-
tenant emit wraps in try/except, failures retry next tick) means
KMS throttling produces a degraded — not broken — anchor cadence.

### Option B — One chain per tenant + shared deployment-wide signing key

Each tenant gets a chain (same as Option A), but all anchors are
signed with the same deployment-wide key from #43.

**Cheaper.** No N-fold KMS-call increase; the cron unwraps the key
once at start-of-pass.

**Weaker isolation.** An operator who unwraps the shared key can
forge ANY tenant's anchor. The whole point of v1.2 (separation of
duties between tenants) collapses. The shared-key-compromise
threat is the same as v1.1, but with per-tenant chains the tenant
NOW has the false expectation of independent verification — when
in fact the shared key is the single forgery point. **Misleading
operators is worse than admitting the limitation.**

**Why we rejected:** the ISO 27001 sub-controls listed in §"Why we
do this" specifically demand tenant-level independence; Option B
delivers chain isolation without verification-key isolation, which
fails the audit conversation.

### Option C — Shared chain + per-tenant classification + per-tenant anchor view

Keep the existing shared `prev_hash` chain; add a per-tenant Merkle
leaf such that anchors prove "tenant X had row Y..." via the leaf
without exposing other tenants' content. The tenant's verifier
receives the tenant's rows + the Merkle path connecting them to the
shared anchor.

**Mathematically elegant.** No schema migration on `audit_logs`,
no per-tenant cron iteration. Tenant verification is a Merkle-path
proof instead of a chain re-computation.

**Operationally heavy.** Requires materializing a Merkle tree per
anchor over potentially millions of rows; rebuilding it on every
export hits storage + CPU hard. Verifier complexity goes up
substantially (the v1.1 verifier is "re-compute prev_hash row-by-
row" — simple to audit; Merkle-path verification is harder to get
right). The hash-window classification mechanism from #43 already
sits awkwardly on top of the chain; adding Merkle proofs would
need a third layer.

**Why we rejected:** the engineering cost of correct Merkle-tree
construction + per-export proof materialization is high, the
verifier complexity is a step backwards from "re-compute the chain
linearly," and the shared-chain property still means the operator
has unilateral chain-rewrite power. Option C trades operational
simplicity (Option A's biggest cost) for cryptographic complexity
that doesn't buy more isolation than Option A delivers.

### Decision

**Option A locked.** Strongest isolation. Linear engineering cost.
Reuses the existing #43 anchor primitive (sign + verify per chain
head) with `(org_id, key_version, chain_head_hash)` as the new
identity tuple instead of `(key_version, chain_head_hash)`. The
KMS-call cost is a known concern, mitigated by §5 decision 8's
batching + failure-safe retry behaviour. Engineering effort is
~equivalent to extending #43 from "one chain" to "N chains" — most
of the helpers stay; the cron grows a per-tenant loop and the
verifier grows an org-scoped accessor.

## Locked decisions

### 1. Schema — extend `audit_anchors` + `audit_signing_keys` with nullable `org_id`

Reuse the existing tables. Add one nullable `org_id` column to each.
`NULL` = shared / deployment-wide (v1.1 backward compat); non-NULL =
per-tenant.

```sql
ALTER TABLE audit_anchors
    ADD COLUMN org_id VARCHAR(36) NULL REFERENCES organizations(id);
CREATE INDEX ix_audit_anchors_org_signed_at
    ON audit_anchors(org_id, signed_at);

ALTER TABLE audit_signing_keys
    ADD COLUMN org_id VARCHAR(36) NULL REFERENCES organizations(id);
CREATE UNIQUE INDEX ux_audit_signing_keys_org_active
    ON audit_signing_keys(org_id) WHERE retired_at IS NULL;
-- (Partial unique index: at most one active key per org. For SQLite
-- test paths, application-side guard substitutes; see §5 decision 8.)
```

**Why not a new `tenant_audit_chains` table?** Considered. Rejected
because (a) the per-tenant rows are byte-for-byte the same shape as
the shared rows; (b) splitting into two tables means every read path
needs `UNION ALL` boilerplate; (c) the partial-index on `org_id`
keeps the lookup-by-active-key cost O(log N) per tenant.

**Why nullable `org_id`?** Pre-#208 rows have NULL, meaning "shared
deployment chain." Post-#208 rows with `isolation_mode='isolated'`
have non-NULL `org_id`, meaning "tenant chain." Non-isolated tenants
in a #208 deployment STILL use the shared chain (NULL anchors) per
decision 6 below. The NULL/non-NULL distinction is a first-class
chain-routing signal, not a "missing data" sentinel.

### 2. Chain-head storage — new `tenant_audit_state` table (one row per isolated org)

A separate small table tracking the per-tenant chain's current head +
the cutover boundary. Lives next to `tenant_states` (existing #36
table) but doesn't merge with it because `tenant_states` is
reconciler-owned and we don't want audit cron writes to fight with
reconciler writes on the same row.

```sql
CREATE TABLE tenant_audit_state (
    org_id            VARCHAR(36) PRIMARY KEY REFERENCES organizations(id) ON DELETE CASCADE,
    chain_started_at  TIMESTAMP NOT NULL,     -- cutover row's created_at
    current_head_hash VARCHAR(64) NOT NULL,   -- hex SHA-256, updated post-write
    last_anchor_at    TIMESTAMP,              -- nullable; NULL before first daily anchor
    lifecycle         VARCHAR(16) NOT NULL DEFAULT 'active',
                                              -- 'active' | 'sealed' (org soft-deleted)
    created_at        TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMP NOT NULL DEFAULT NOW()
);
```

**Why not a column on `tenant_states`?** Reconciler write cadence
(every 5 min sweep + on-event) is different from audit-write
cadence (every audit_logs INSERT updates `current_head_hash`); the
hot-path coupling would force the reconciler to handle audit
locking. Separation keeps each table's writers small + single-purpose.

**Why not a Redis key?** Considered. Rejected because (a) the chain
head MUST survive Redis restarts — a missed head means the next
row chains to GENESIS, breaking verification; (b) Redis is optional
in the deployment (single-replica mode runs without Redis per
[multi-replica.md](../../docs/docs/concepts/multi-replica.md)); (c)
the audit write already touches the DB so an extra UPDATE on
`tenant_audit_state` is cheap. Redis becomes a **cache** (key
`aifactory:tenant-chain-head:<org-uuid>`) populated from the DB on
miss, invalidated on update — covered in §5 decision 9.

### 3. AuditLog write path — chain to per-tenant head when `org_id` matches an isolated tenant

The existing `audit_service.py` writes use the deployment-wide chain
head from the most-recent `audit_logs` row. Change:

```python
async def write_audit_row(db, *, org_id, action, ...):
    if org_id and await _is_tenant_isolated(db, org_id):
        # Per-tenant chain.
        head = await _load_tenant_chain_head(db, org_id)  # uses Redis cache + DB fallback
        new_row = AuditLog(..., prev_hash=head)
        # Compute the row's outgoing hash and update the tenant state in the SAME tx.
        outgoing = compute_hash(head, row_as_mapping(new_row))
        await _bump_tenant_chain_head(db, org_id, outgoing)
    else:
        # Shared chain (v1.1 path, unchanged).
        head = await _load_shared_chain_head(db)
        new_row = AuditLog(..., prev_hash=head)
    db.add(new_row)
    await db.flush()
```

**Why `_is_tenant_isolated` reads `tenant_states.isolation_mode`?**
The chain-mode decision is keyed to the same flag the reconciler
uses to provision K8s/IAM/Vault resources. A tenant flipped from
`shared` → `isolated` at time T sees the cutover happen at the FIRST
audit-log write after T. The `tenant_audit_state` row is upserted
with `chain_started_at = NOW()` + `current_head_hash = 'GENESIS-T-<org-uuid>'`
at that moment. **First write after isolation flip is the chain
genesis.** Documented in the concept doc so operators don't expect
pre-isolation rows to retro-chain.

**Why update tenant_audit_state in the same tx?** A failed audit
write that committed the row but not the head update leaves the
next write chaining to a stale head — silent verification failure.
Same-tx atomicity is the only safe contract.

**Cache invalidation race (§5 decision 9):** the Redis cache for
the head is invalidated AFTER the DB commit succeeds. If two pods
race to write rows for the same tenant: each pod reads the head,
writes its row, updates the DB head, invalidates Redis. The second
pod's write fails the row's prev_hash uniqueness via an advisory
lock — see §5 decision 9 for the SELECT ... FOR UPDATE detail.

### 4. Chain genesis encoding — per-tenant sentinel `GENESIS-T-<org-uuid>`

The shared chain uses `GENESIS` as the prev-hash sentinel for the
first row. Per-tenant chains use `GENESIS-T-<org-uuid>` (literal
prefix + UUID). The verifier resolves the sentinel from the row's
`org_id` column at verification time:

```python
def expected_genesis_for(row) -> str:
    if row.get("org_id") and row["_chain_mode"] == "tenant":
        return f"GENESIS-T-{row['org_id']}"
    return GENESIS
```

**Why prefix the UUID?** Three reasons:
1. Makes the chain-mode discriminator visible in the row data —
   a verifier handed a single row can tell at a glance which chain
   rules apply.
2. Prevents accidental cross-chain hash collisions if some attacker
   tries to splice a per-tenant chain segment into the shared
   chain or vice versa (the first row's prev_hash wouldn't match
   either chain's expected genesis).
3. Makes log-grep + DB-query for "where did tenant X's chain
   start?" trivial: `SELECT id, created_at FROM audit_logs WHERE
   prev_hash = 'GENESIS-T-<uuid>'`.

**Canonical-encoding compatibility:** the chain-head computation in
[audit_chain.py](../../apps/web-server/server/services/audit_chain.py)
treats `prev_hash` as an opaque string. The `GENESIS-T-<uuid>`
sentinel is a valid opaque string and produces a unique hash. No
change to `_canonical()`; the chain helper just sees a different
sentinel for the first row.

### 5. Anchor cron — single pass writes N anchors per day (one per isolated tenant + one shared)

The existing daily cron at 00:00 UTC currently writes one anchor
covering the shared chain. Post-#208 it iterates over every tenant
in `isolation_mode='isolated'` plus one shared-chain anchor (for
the non-isolated tenants + pre-cutover rows).

```python
async def run_once_for_today(db):
    today = _utc_today()
    yesterday = today - timedelta(days=1)

    # 1. Shared chain anchor — covers non-isolated tenants + pre-cutover rows.
    await emit_shared_anchor_for_day(db, yesterday)

    # 2. Per-tenant anchors — one per isolated org.
    iso_orgs = await _select_isolated_orgs(db)
    for org_id in iso_orgs:
        await emit_tenant_anchor_for_day(db, org_id, yesterday)
        # Each call: independent failure-safe wrapper. One tenant's
        # KMS-unwrap-failure or DB-IntegrityError does NOT abort the loop.
```

**Why one cron pass, not one cron per tenant?** A Kubernetes
CronJob per tenant scales linearly with operator overhead (10k
CronJob CRs in a cluster is awkward; some clusters' API servers
struggle past 1000 CronJobs). One pass per cluster keeps the
operator surface flat: `kubectl get cronjob audit-anchor` shows one
job whose logs cover every tenant.

**Batching for KMS rate-limit safety:** the per-tenant loop unwraps
each tenant's signing key. With 10k tenants in one pass that's 10k
KMS unwraps in a tight loop. The implementation uses an in-process
LRU cache (size = `audit.anchor.perTenant.keyCacheSize` in
`values.yaml`, default 1000) so re-runs within the cache TTL skip
the KMS round-trip. First-run-after-pod-restart for 10k tenants
takes ~10k × KMS-RTT (typically 20-50ms for AWS KMS) = 200-500
seconds — acceptable for a daily job that doesn't need to complete
in seconds. The cron's per-tenant emit has its own try/except so
KMS throttling errors retry on the next day's pass; the failed
tenants land in `tenant_audit_state.last_anchor_at` staying stale,
which the health-check query catches.

**Sequencing within the loop:** isolated orgs are processed in
`Organization.id` order (stable, deterministic). A failure on org
X doesn't block org Y. The cron logs the per-tenant outcome at INFO
("emitted", "skipped (already exists)") or WARNING ("KMS unwrap
failed, will retry next tick"). Operator-visible aggregate metric:
`audit_anchor_emit_total{org_id, status}` exposed via the existing
Prometheus surface.

### 6. Backward compatibility — non-isolated tenants keep shared chain

Three regimes coexist:

| Org status (post-#208) | Chain mode | Anchor | Rationale |
|---|---|---|---|
| Pre-#208 (no `tenant_states` row at all, OR row but `isolation_mode='shared'`) | Shared | Shared deployment anchor (v1.1) | Zero behaviour change for existing operators; no opt-in needed |
| Post-#208, `isolation_mode='isolated'`, isolation flip happened at time T | Pre-T rows: shared / Post-T rows: per-tenant | Pre-T: shared anchor / Post-T: per-tenant anchor | Avoids destructive rewrite of historical rows; verifier reads two segments |
| Post-#208, `isolation_mode='deleted'` (org soft-deleted) | Per-tenant chain sealed; new rows refused | Per-tenant anchor stops at seal time (`tenant_audit_state.lifecycle='sealed'`) | Tenant's chain stays verifiable forever; no new rows can be appended |

**Helm opt-in flag:** `audit.anchor.perTenant=true` in `values.yaml`
gates the whole feature. When `false` (default for pre-#208
upgrades), the per-tenant cron loop short-circuits and the audit
write path always uses the shared chain (even for
`isolation_mode='isolated'` tenants). This decouples #208's
rollout from #36's: operators on #36 v1.1 isolated mode keep
working byte-for-byte; flipping `audit.anchor.perTenant=true` at a
later release enables the per-tenant chain prospectively from the
flip moment. Documented operator runbook in PR-3.

**When both flags are true but `isolation_mode='shared'` for an
org:** that org stays on the shared chain. Per-tenant chain is
**bound to** isolated-mode tenants, not orthogonal to them.
Rationale: the per-tenant chain's whole security story depends on
the tenant's Vault path (where the wrapped key lives, §5 decision 7),
which only exists for isolated tenants under #36.

### 7. Per-tenant key storage — `audit_signing_keys.org_id` + mirror to tenant Vault path

The KMS-wrapped raw key lives in `audit_signing_keys` (per §1).
**Plus** the reconciler writes the same wrapped blob to the
tenant's Vault path on issuance:

```
aifactory/orgs/<org-uuid>/anchor-key-wrapped     (the KMS-wrapped 32-byte blob)
aifactory/orgs/<org-uuid>/anchor-key-metadata    (JSON: { version, created_at, kms_key_id })
```

**Why both?** The DB-side row is what the cron uses (low-latency
read, transactional with the rest of the schema). The Vault-side
copy is what the **tenant's auditor** uses: when the operator runs
the audit-handover runbook (`vault kv get
aifactory/orgs/<org-uuid>/anchor-key-wrapped`), the auditor receives
the wrapped blob + the KMS root-key identifier and unwraps via the
operator's KMS backend on a one-shot basis (the runbook documents
the exact KMS API call sequence per backend). The auditor never
needs DB access.

**Trust scope:** the operator still mediates KMS unwrap (the
auditor doesn't get raw KMS access). What changes is the operator
unwraps **only the tenant's key**, not the deployment-wide key,
satisfying tenant-scoped need-to-know. The reconciler's Vault
AppRole (`aifactory-reconciler` per #36 §5) already has
write-but-not-read access to `aifactory/orgs/<org-uuid>/*`, which
is exactly what we need here.

**Reconciler trigger:** the FIRST reconcile pass for an
`isolation_mode='isolated'` tenant generates the per-tenant key
(via the existing `audit_anchor.generate_new_key()`), wraps it via
the KMS backend, INSERTs the `audit_signing_keys` row with
`org_id=<org-uuid>`, then mirrors the wrapped blob to the Vault
path. All three operations in the same reconcile try/except — a
failure leaves no half-state because the `tenant_audit_state` row
isn't created until all three succeed. The audit write path checks
`tenant_audit_state` existence before assuming per-tenant chain
mode; absence means "key not yet issued, fall back to shared chain
for this row" with a WARNING log. Eventual consistency: next
reconcile pass tries again.

### 8. Concurrency — SELECT ... FOR UPDATE on `tenant_audit_state` per write

Postgres' `SELECT ... FOR UPDATE` on the tenant's `tenant_audit_state`
row before reading `current_head_hash` guarantees serialised access
to the head within a tx. Two concurrent writers for the same org
serialise; the second waits for the first's commit, reads the
updated head, chains correctly.

```python
async def _load_tenant_chain_head_locked(db, org_id):
    # FOR UPDATE blocks other writers on the same org_id row.
    stmt = select(TenantAuditState).where(
        TenantAuditState.org_id == org_id,
    ).with_for_update()
    state = (await db.execute(stmt)).scalar_one()
    return state.current_head_hash
```

**SQLite test paths:** SQLite serialises all writes anyway (one
writer at a time per database). The `with_for_update()` becomes a
no-op on SQLite. Same chain correctness, no extra mechanism needed.

**Why not a Redis distributed lock?** Two reasons:
1. The DB row-lock is in-tx with the audit write itself, so the
   lock release is atomic with the commit. A Redis lock + DB write
   has a window where the Redis lock releases before the DB commit
   visible to other readers — race condition.
2. The audit hot-path already touches the DB; an extra Redis
   round-trip per audit write is observable overhead.

**Redis stays in the picture (per §3) only as a non-authoritative
cache** of the head, invalidated on update. Reads consult Redis
first; on miss or write, the DB `FOR UPDATE` path runs.

### 9. Verifier — `aifactory audit verify-anchor --org-id <uuid>` CLI tool

New entry point in the existing
[server/audit/__main__.py](../../apps/web-server/server/audit/__main__.py)
that:

1. Loads `tenant_audit_state` for the org → finds `chain_started_at`,
   `current_head_hash`, `lifecycle`.
2. Loads all `audit_signing_keys` rows with `org_id=<uuid>` → for
   each, calls the existing KMS backend's `decrypt(wrapped_key)`
   to get the raw 32 bytes; builds the `key_version → raw_key` map.
3. Reads `audit_logs` rows with `org_id=<uuid>` AND `created_at >=
   tenant_audit_state.chain_started_at`, ordered by `created_at ASC`.
4. Reads `audit_anchors` rows with `org_id=<uuid>` ordered by
   `signed_at ASC`.
5. Calls a new `verify_tenant_anchored_export(rows, anchors,
   signing_keys, org_id)` helper that's a thin wrapper over the
   existing
   [verify_anchored_export](../../apps/web-server/server/services/audit_export.py)
   with the per-tenant genesis sentinel (§4 decision 4).
6. Prints PASS / FAIL + count of rows / anchors verified + any
   chain breaks.

**The external auditor's workflow:**

```bash
# 1. Operator exports tenant's rows + anchors:
$ aifactory audit export --org-id <uuid> --format ndjson --include-anchors > tenant-export.ndjson

# 2. Operator retrieves the wrapped key from Vault:
$ vault kv get -format=json aifactory/orgs/<uuid>/anchor-key-wrapped > wrapped-key.json

# 3. Operator unwraps via KMS (one-shot, audited):
$ aifactory kms unwrap --backend aws-kms < wrapped-key.json > raw-key.bin

# 4. Auditor verifies offline:
$ aifactory audit verify-anchor --org-id <uuid> --export tenant-export.ndjson --key raw-key.bin
PASS: 12,438 rows + 31 anchors verified against tenant chain
```

The `kms unwrap` step is the only one requiring operator KMS
access; everything else is data-only. The runbook in
`guides/compliance/per-tenant-audit-handover.md` (new in PR-3)
documents the operator's audit-log entries for this flow
(`audit.handover.tenant-key.unwrap` at
`classification='confidential'`).

### 10. Anchor cron entry per tenant — `(org_id, signed_at_date)` uniqueness

The existing partial-unique-on-date constraint from #43 covers
`audit_anchors(DATE(signed_at AT TIME ZONE 'UTC'))` for the shared
chain. The per-tenant extension changes this to:

```sql
-- Replaces the v1.1 constraint.
CREATE UNIQUE INDEX ux_audit_anchors_org_day
    ON audit_anchors(org_id, DATE(signed_at AT TIME ZONE 'UTC'));
-- WHERE clause handles the NULL=shared case correctly in Postgres
-- (NULL is treated as distinct, but the partial-unique style means
-- (NULL, 2026-05-29) and (NULL, 2026-05-29) ARE caught — Postgres
-- treats NULLs as not-equal by default; we work around with
-- COALESCE in a CHECK).
ALTER TABLE audit_anchors
    ADD CONSTRAINT ck_audit_anchors_org_day_unique
    UNIQUE (COALESCE(org_id, '00000000-0000-0000-0000-000000000000'),
            DATE(signed_at AT TIME ZONE 'UTC'));
```

**Why both an index AND a CHECK?** The index is the read-path
optimization for per-tenant anchor lookup. The CHECK constraint
handles the NULL-equal-NULL semantics correctly without depending
on Postgres NULL-distinct quirks across versions.

**Idempotency:** the cron's existing IntegrityError catch handles
both: a duplicate `(org_id, date)` 409s; the cron logs "lost race"
and continues.

## Reviewer-style audit pass — 6 critical findings + 5 recommendations

### Critical findings (must address before / during implementation)

**Finding #1 — KMS rate-limit at 10k+ tenants.** The cron loop's
per-tenant unwrap is the single highest KMS-call-rate hotspot.
AWS KMS regional rate is 5,500 ops/sec across all KMS API verbs
sharing the quota; a daily pass over 10k tenants is well inside
the 1-sec-per-1k-tenants regime but a deployment doing other KMS
work (every encrypted-column read uses KMS for data-key unwrap —
see [P2 design](../../apps/web-server/server/database/models.py)
`kms_data_keys`) hits the shared quota. Mitigation locked in §5
decision 8's in-process LRU cache (default size 1000); operators
at 10k+ tenants set `audit.anchor.perTenant.keyCacheSize=10000` and
accept the proportional memory cost (~320 KB at 10k × 32 raw
bytes + overhead). **PR-3 must document the operator-tunable knob
+ the regional KMS quota math.**

**Finding #2 — Anchor write cost at 10k tenants.** 10k anchor
INSERTs per day in a tight cron loop = ~10k DB writes within
minutes of 00:00 UTC. The write itself is small (~200 bytes per
row), so the raw IO cost is fine; the concern is the lock + WAL
churn. The existing shared anchor writes one row per day; this
goes to N rows per day. Mitigation: the cron's per-tenant loop
commits in batches of `audit.anchor.perTenant.batchSize` (default
100) — every 100 tenants the cron commits + flushes the WAL.
Failure within a batch rolls back that batch; other batches stay
committed. **PR-2 must implement batch boundaries; PR-3 must
expose the value.**

**Finding #3 — Chain orphans on org delete (#36 30-day grace).**
The #36 stage-1 soft-delete sets `Organization.deleted_at` and
flips `tenant_states.isolation_mode='deleted'`; the audit cron
keeps emitting anchors against the org's chain head (no new rows
because the agent spawner refuses new tasks, but the head still
exists). At stage-2 (day 30), the namespace + S3 + Vault tear
down. **The audit chain rows + the `tenant_audit_state` row + the
`audit_signing_keys` row MUST survive the tear-down** — they are
the legal-hold artefact. The PR-3 docs lock this: the tenant
reconciler's stage-2 tear-down EXPLICITLY skips the audit tables;
it only marks `tenant_audit_state.lifecycle='sealed'` and DELETEs
the Vault `anchor-key-wrapped` path (so the wrapped blob lives
only in the DB after seal). The DB-side `audit_signing_keys` row
stays forever so the historical anchor stays verifiable; the
Vault-side copy is removed to satisfy "no remaining handover
path" once the tenant relationship terminates. **PR-1 must add a
test asserting tear-down leaves audit rows intact.**

**Finding #4 — Stuck-tenant edge: tenant with zero rows in past
N days.** A tenant that hasn't written any audit rows since their
last anchor still gets an anchor every day with the same
`current_head_hash`. Same behaviour as the shared chain's
zero-row-day rule in #43 §2. **Lock: emit the anchor with
the unchanged head; verifier sees consecutive identical anchors as
"quiescent day" evidence.** Honest acknowledgement of the cost:
10k tenants × 1 anchor/day = 10k rows/day even if no audit activity
happens. Over a year that's 3.65M rows. Partitioning the table by
date isn't in v1.2 scope; PR-3 docs note the growth rate +
operator runbook for retention-driven pruning past a configurable
horizon (decision: `audit_anchors` rows older than
`audit.anchor.retentionDays` (default: 1825 = 5 years) are eligible
for retention-job cleanup, with a STRONG warning that pruning
discards the historical verification capability for that window).

**Finding #5 — Race: row written for org A while cron is anchoring
org A.** The cron reads `tenant_audit_state.current_head_hash` and
signs it; meanwhile an audit write arrives, takes the `FOR UPDATE`
lock on the same row, updates the head, commits. The cron's
already-signed anchor now references a head that's no longer the
latest. **Lock the resolution at design time:** the cron acquires
`SELECT ... FOR UPDATE` on `tenant_audit_state` for the duration
of the per-tenant emit. New audit writes for that tenant block on
the lock for the (tiny) emit duration. After commit, audit writes
proceed normally and chain to the post-anchor head. The verifier's
"pending window" semantics from #43 apply unchanged: rows whose
`created_at > anchor.signed_at` form the pending window for the
next anchor. **PR-2 test: concurrent-writer + cron race with
deterministic ordering check.**

**Finding #6 — Verifier needs the per-tenant key — operational
ergonomics.** The runbook in §5 decision 9 has 4 manual steps for
the auditor handover. Each step is auditable + logged, which is
the security property we want; but every step is also a chance
for the operator to mis-paste the wrong tenant's key (the wrapped
blob is opaque; you can't visually tell tenant A's from tenant B's).
**Lock: the wrapped blob's filename + the auditor's CLI's
`--org-id` argument MUST match; the verifier asserts the
`audit_signing_keys.org_id` of the loaded key matches the
`--org-id` argument and FAILS LOUDLY otherwise.** Documented in
the runbook as the operator's primary safety check.

### Recommendations (improve but not blocking)

**Recommendation #1 — Per-tenant Prometheus metrics.** Expose
`audit_anchor_emit_total{org_id, status}` so operators can alert
on "tenant X has had no successful anchor emit for 48h." Counter
label cardinality at 10k tenants is high but bounded; operators
running tight Prometheus budgets can disable per-org labels via a
chart value (`audit.anchor.perTenant.metrics.perOrgLabels=false`).

**Recommendation #2 — Index `audit_logs(org_id, created_at)`.**
The audit write path's `_load_tenant_chain_head` query needs the
latest row per org. Add a composite index. The existing
`ix_audit_logs_org_id` + `ix_audit_logs_created_at` are separate
single-column indexes; the composite is faster for the
"order-by-created_at filtered-by-org" hot path. PR-1 migration.

**Recommendation #3 — Document the Vault AppRole capability
addition.** The #36 reconciler AppRole has write on
`aifactory/data/orgs/aifactory-tenant-*/*` (per #36 §5 — but
re-check; the actual capability is on `auth/kubernetes/role/...`
and `sys/policies/acl/...`). PR-3 must add the data-write
capability for the `anchor-key-wrapped` + `anchor-key-metadata`
paths: `path "aifactory/data/orgs/+/anchor-key-*" { capabilities
= ["create", "update"] }`. NO read — the reconciler writes but
doesn't read tenant secrets, preserving the #36 trust model.

**Recommendation #4 — Per-tenant export endpoint shape.** The
existing `/api/admin/audit/export?org_id=<uuid>&include_anchors=true`
already 400s today (per
[audit_export.py:188-192](../../apps/web-server/server/services/audit_export.py))
because the shared anchor can't verify a filtered export. PR-3
updates the route to accept the combination when the tenant has
`isolation_mode='isolated'` AND `audit.anchor.perTenant=true`,
because the per-tenant chain CAN verify a per-tenant export. The
400 stays for non-isolated tenants. Documented behaviour change
in the v1.2 CHANGELOG.

**Recommendation #5 — Reconciler health check for "key issued but
no first anchor yet."** Adds a query for the
`tenant_audit_state.lifecycle='active' AND last_anchor_at IS NULL
AND created_at < NOW() - INTERVAL '48 hours'` condition to the
existing reconciler-error reporting. Catches the rare bug where
the key is issued but the cron never picked the tenant up
(misconfigured `audit.anchor.perTenant=true`, cron crash before
first daily tick, etc.). PR-2.

## Implementation plan — 3 PRs

Same shape as the v1.1 #43 PR series. Each PR is sized for a
single review pass + atomic merge.

### PR-1 — Schema + per-tenant key issuance via reconciler hook

- Alembic migration:
  - `audit_anchors.org_id` (nullable FK to `organizations.id`).
  - `audit_signing_keys.org_id` (nullable FK to `organizations.id`).
  - Partial unique index on `audit_signing_keys(org_id) WHERE
    retired_at IS NULL`.
  - New table `tenant_audit_state` (per §1 decision 2 schema).
  - Composite index `audit_logs(org_id, created_at)` (rec #2).
  - Drop + recreate the v1.1 unique-on-date constraint into the
    new `(org_id, DATE(signed_at AT TIME ZONE 'UTC'))` per §5
    decision 10.
- ORM models: `TenantAuditState`; extend `AuditAnchor` +
  `AuditSigningKey` with `org_id` + `organization` relationship.
- `apps/web-server/server/services/tenant_reconciler.py` — hook in
  `_apply_create_or_update`:
  - After K8s + IAM + Vault writes succeed AND
    `audit.anchor.perTenant` flag is on, call
    `await issue_tenant_anchor_key(db, vault_client, org)`.
  - New helper module
    `apps/web-server/server/services/per_tenant_anchor_key.py`
    encapsulates: generate raw key → wrap via existing KMS backend
    → INSERT `audit_signing_keys` row → write wrapped blob to
    Vault `aifactory/orgs/<uuid>/anchor-key-wrapped` →
    upsert `tenant_audit_state` row with `chain_started_at=NOW()`,
    `current_head_hash='GENESIS-T-<uuid>'`, `lifecycle='active'`.
- Tests:
  - Migration applies cleanly + is reversible.
  - Reconciler issuance hook idempotent (re-run = noop, no duplicate
    rows).
  - Key generation produces exactly 32 raw bytes; wrap+unwrap
    round-trip via every supported KMS backend (mirrors #43's
    backend matrix).
  - Vault write uses the correct AppRole capability (mocked
    `vault_client` asserts the path + the absence of read
    capability).
  - Tear-down preserves audit rows (finding #3 acceptance).

### PR-2 — Per-tenant chain helper + cron extension + verifier helper update

- `apps/web-server/server/services/audit_chain.py` — add
  `expected_genesis_for(row, chain_mode)` helper (§4 decision 4).
- `apps/web-server/server/services/audit_service.py` — modify the
  write path per §3 decision 3 (`_is_tenant_isolated` lookup +
  per-tenant head load + same-tx head bump). Cache via Redis when
  `redis.enabled=true`; DB-only fallback otherwise.
- `apps/web-server/server/jobs/audit_anchor_cron.py` —
  `emit_tenant_anchor_for_day(db, org_id, day)` plus the
  per-tenant iteration loop in `run_once_for_today`. Per-tenant
  emit wraps in try/except; batch commits every
  `audit.anchor.perTenant.batchSize` (finding #2).
- `apps/web-server/server/services/audit_export.py` — new
  `verify_tenant_anchored_export(rows, anchors, signing_keys,
  org_id)` helper. Re-uses 90%+ of the existing
  `verify_anchored_export` body; differs only in the genesis
  sentinel resolution.
- `apps/web-server/server/audit/__main__.py` — new
  `verify-anchor --org-id <uuid>` subcommand (§5 decision 9).
- `tests/audit/test_per_tenant_chain.py`:
  - Audit write for isolated tenant produces tenant-chained
    `prev_hash`; first row uses `GENESIS-T-<uuid>`.
  - Audit write for shared tenant unchanged.
  - Cron emits N+1 anchors per day (N isolated + 1 shared); each
    verifies independently with its own key.
  - Concurrent-writer + cron race (finding #5): two pods writing
    rows for the same isolated tenant + cron emitting in parallel,
    final chain verifies end-to-end.
  - Per-tenant verifier rejects wrong key (`--org-id A` with
    `--key B-raw.bin` FAILS LOUDLY per finding #6).
  - Verifier handles the pre-cutover/post-cutover boundary
    correctly for an org migrated from shared to isolated
    mid-deployment.
- Health-check query for "key issued but no first anchor yet"
  (rec #5).

### PR-3 — Helm `audit.anchor.perTenant` toggle + concept doc update + ISO 27001 evidence update

- `charts/aifactory/values.yaml`:
  ```yaml
  audit:
    anchor:
      enabled: false              # v1.1 toggle (existing)
      perTenant: false            # v1.2 toggle (this PR)
      perTenantOptions:
        keyCacheSize: 1000        # finding #1
        batchSize: 100            # finding #2
        retentionDays: 1825       # finding #4 (5 years)
        metrics:
          perOrgLabels: true      # rec #1
  ```
  Validators reject `perTenant=true` without `enabled=true`.
- `charts/aifactory/templates/cronjob-audit-anchor.yaml` — no
  shape change (still one CronJob); the per-tenant iteration is
  in-pod.
- `charts/aifactory/templates/vault-policy-aifactory-reconciler.yaml`
  (or the equivalent operator-applied Terraform sample) — extend
  the AppRole's capability list per rec #3 (`path
  "aifactory/data/orgs/+/anchor-key-*" { capabilities = ["create",
  "update"] }`).
- `tests/helm/test_audit_anchor_per_tenant_toggle.py`.
- `docs/docs/concepts/audit-anchor.md` — append the
  "Per-tenant chains (v1.2)" section: how the flag works, the
  three-regime backward-compat table from §6, the auditor handover
  flow + sample commands from §5 decision 9, the threat-model
  delta from §"Threat model" below.
- `docs/docs/concepts/tenant-isolation.md` — append a cross-link
  to the audit-anchor doc's per-tenant section + note that
  `audit.anchor.perTenant=true` is recommended-but-not-required
  for isolated-mode tenants.
- `guides/compliance/per-tenant-audit-handover.md` — new runbook
  for the operator (4-step auditor handover from §5 decision 9,
  including the per-step audit-log entries the operator's actions
  produce).
- `guides/compliance/iso27001-evidence.md` — update A.12.4.2 +
  A.12.4.3 + A.18.1.3 + A.18.2.2 control entries to cite the
  per-tenant chain when `audit.anchor.perTenant=true`. Each entry
  is marked "available in v1.2+; v1.1 deployments evidence
  deployment-wide chain only."
- `CHANGELOG.md` v1.2 entry: closes #208 (per-tenant audit anchor),
  notes the recommendation #4 behaviour change (export endpoint
  now accepts org-filtered + anchors combo for isolated tenants).
- Update the v1.1 audit-anchor design doc
  ([2026-05-28-audit-anchor-design.md](2026-05-28-audit-anchor-design.md))
  with the same "shipped 2026-Mxx-DD — per-tenant superseded by
  #208" status stamp pattern used for #43.

## Failure-safe contract

Same v1.1 pattern. Every per-tenant code path wraps in try/except.
Specific failure modes + responses:

| Failure | Behaviour |
|---|---|
| Per-tenant KMS unwrap fails for one tenant during cron | That tenant's anchor skipped; WARNING logged; cron continues to next tenant; retried next tick |
| `tenant_audit_state` row missing on audit write | Fall back to shared chain for THIS row; WARNING logged; reconciler will eventually create the row |
| Audit write succeeds but head bump fails | TX rolls back; row not committed; caller sees error; retried by upstream caller per existing audit-service contract |
| Vault write of wrapped key fails | `audit_signing_keys` row stays (DB-side copy is authoritative); reconciler retries Vault write next tick; the DB row + auditor-runbook-from-DB still works as a fallback |
| Per-tenant cron loop crashes mid-pass | Crash exits the asyncio task; lifespan supervisor restarts; next pass picks up where the per-tenant emit failed (each is independent + idempotent via the unique-on-day constraint) |
| Cache invalidation race | Worst case: a write chains to a stale head; the same-tx `SELECT ... FOR UPDATE` makes this impossible within Postgres; under SQLite the single-writer semantics serialise; cache stays a non-authoritative read accelerator |

The reconciler + the audit cron never crash the web pod. Health-
check queries (`tenant_audit_state.lifecycle='active' AND
last_anchor_at < NOW() - INTERVAL '36 hours'` for stuck-tenant
detection) surface degradation without taking the system down.

## Threat model

| Threat | Pre-#208 (v1.1 #43 shared anchor) | Post-#208 (v1.2 per-tenant anchor, `isolation_mode='isolated'`) |
|---|---|---|
| Tenant A's auditor verifies tenant A's audit log without seeing tenant B's data | **Undefended** (shared anchor signs cross-tenant chain head; per-org export loses chain verifiability) | **Defended** (per-tenant chain + per-tenant anchor; per-org export verifies independently) |
| Operator with DB write access rewrites tenant A's chain | Defended unless operator also has shared HMAC key (single key compromise covers all tenants) | Defended unless operator also has TENANT A's HMAC key (compromise scoped to one tenant) |
| Operator forges tenant A's anchor to hide an event | Possible if operator unwrapped shared key | Possible only if operator unwrapped tenant A's specific wrapped key from tenant A's Vault path; KMS unwrap is auditable per existing #43 pattern; cross-tenant forgery requires N unwraps of N different keys |
| Tenant B's auditor learns tenant A exists from a deployment-wide export | **Undefended** (deployment export is the only verifiable form; tenant B sees tenant A's rows) | **Defended** (per-tenant export is verifiable; tenant B never sees tenant A's chain) |
| Compromised KMS root key | All anchors forgeable | All anchors forgeable (same — per-tenant keys are wrapped by the same root) |
| Tenant A's compromised pod reads tenant A's chain key | N/A — there is no per-tenant key | Defended — pod doesn't have Vault read on `aifactory/orgs/<uuid>/anchor-key-wrapped` (the reconciler AppRole has write-not-read; the tenant SA's policy is read-only on `aifactory/orgs/<uuid>/*` MINUS `anchor-key-*` — added in PR-3) |
| Cron compromise forges yesterday's anchor for every tenant in one pass | Already possible (cron has the shared key in memory) | Still possible per tenant (cron must unwrap every per-tenant key; compromise scope = N keys instead of 1, but the cron has the access by construction); mitigated by KMS-side audit trail of N unwraps in a tight loop being detectable |
| External attacker tampers with exported NDJSON between download + auditor | Detected — anchor verifies the chain head (v1.1 unchanged) | Detected — per-tenant anchor verifies tenant chain head (same property scoped to one tenant) |

**Notable new property in v1.2:** tenant-scoped independence. An
operator who wants to rewrite tenant A's history must unwrap tenant
A's specific key. The unwrap is KMS-audited (per the existing KMS
backend's logging) and visible to the tenant via the operator's
disclosure obligation (operator runbook + the audit-log entry
`audit.handover.tenant-key.unwrap` from §5 decision 9). The
operator can no longer silently rewrite — the unwrap leaves a
trail the tenant can later request.

**v1.3+ closes the residual:** external publication (S3 Object
Lock per tenant, Sigstore per tenant) writes per-tenant anchors to
storage the operator can't rewrite, fully removing the operator
from the trust path. Same v1.2-to-v1.3 trajectory as v1.1's #43
deferral to v1.2 external pub.

## Open questions

To be resolved at review time or during PR-1:

1. **Vault path naming — `aifactory/orgs/<uuid>/anchor-key-wrapped`
   vs nesting under `aifactory/orgs/<uuid>/audit/anchor-key-wrapped`.**
   Recommendation: flat `anchor-key-wrapped` path. The nested form
   reads better but adds a level the reconciler's existing
   `aifactory/orgs/+/*` patterns don't cover; we'd need a glob
   change to the reconciler AppRole. Flat path is simpler + less
   error-prone.

2. **Reconciler ordering — should anchor-key issuance gate the
   `tenant_states.reconciled_at` timestamp?** If issuance fails but
   K8s+IAM+Vault succeed, is the org "reconciled"? Recommendation:
   YES, mark reconciled when the K8s/IAM/Vault core succeeds; key
   issuance failure goes into `tenant_states.reconcile_error` as a
   warning string. Rationale: an isolated tenant without an anchor
   key still functions for normal operations (agent pods spawn,
   audit rows write — just to the shared chain as a fallback per
   the failure-safe contract). Hard-failing reconcile on anchor-key
   issuance would needlessly block tenant operations on a
   non-critical sub-feature.

3. **Should the cron also emit a "no-op" anchor on day 1 for a
   newly-isolated tenant whose chain just started?** The cron runs
   for "yesterday." If the tenant was isolated at T (today) and
   the cron runs at T+1 00:00 UTC, "yesterday" = T's date. There
   are likely rows from the isolation moment forward; the cron
   emits a normal anchor. If the isolation happened at T-1
   23:59 UTC, the cron at T 00:00 UTC sees almost no per-tenant
   chain — emits an anchor with `chain_head_hash = 'GENESIS-T-<uuid>'`
   (no rows yet) signed under the new key. Acceptable; locks in
   "the tenant chain exists from day 1 forward, even if it's
   empty." Documented in concept doc.

4. **Per-tenant key generation: web-pod-side vs reconciler-side.**
   The reconciler runs in the web pod (per #36 §1) so they're the
   same process. But conceptually, "the reconciler issues the
   key" vs "the audit cron issues the key on first write" is a
   choice. Recommendation: reconciler issues on first reconcile
   pass after isolation flip. Reasoning: the reconciler already
   owns the Vault write capability and the per-tenant-state
   lifecycle; the audit cron should be read-mostly w.r.t. key
   issuance.

5. **What happens if `audit.anchor.perTenant=true` is flipped
   to `false` mid-deployment?** Lock: existing per-tenant chains
   STAY per-tenant; new audit writes from that moment chain to
   the shared chain (because the write path checks the flag).
   The per-tenant `tenant_audit_state.lifecycle` becomes
   effectively-sealed (no new rows append). Verifier still works
   against historical per-tenant rows. PR-3 docs warn this is
   "rarely the right operation" and the recommended path is to
   keep per-tenant on once enabled.

6. **Helm chart — should `audit.anchor.perTenant=true` imply
   `tenant.isolationEnabled=true` via a validator?** Recommendation:
   YES. Per-tenant chains depend on the Vault path that only
   exists for isolated tenants. PR-3 adds a Helm pre-install
   validator that errors with: *"audit.anchor.perTenant requires
   tenant.isolationEnabled — per-tenant anchor keys live in the
   tenant's Vault path which is provisioned only by tenant
   isolation."*

## Decision audit summary

10 of 10 locked decisions taken. Reviewer-style audit pass added
6 critical findings + 5 recommendations, all baked in above:

| Finding | Resolution |
|---|---|
| KMS rate-limit at 10k+ tenants | LRU cache in cron (size = chart value); operator-tunable; KMS quota math documented in PR-3 |
| Anchor write cost at 10k tenants | Batch commits every N tenants (chart value); per-batch rollback isolation |
| Chain orphans on org delete | Audit tables EXCLUDED from #36 stage-2 tear-down; `lifecycle='sealed'`; Vault wrapped-key DELETEd; DB-side wrapped key stays for verifier-from-DB fallback |
| Stuck-tenant edge (zero rows in window) | Emit anchor with unchanged head; quiescent-day semantics from #43 carry over; growth-rate documented; retention-driven pruning behind chart value |
| Race: row write vs cron anchor for same tenant | `SELECT ... FOR UPDATE` on `tenant_audit_state` per-emit; new writes block briefly on lock; cron commits then writes proceed; verifier "pending window" semantics unchanged |
| Verifier loading wrong tenant's key | CLI asserts `audit_signing_keys.org_id == --org-id`; FAILS LOUDLY on mismatch; runbook surfaces this as primary operator safety check |
| Per-tenant Prometheus metrics | Recommended; opt-out via chart value for high-cardinality-sensitive deployments |
| `audit_logs(org_id, created_at)` composite index | Added in PR-1 migration |
| Reconciler AppRole Vault data-write capability | Added in PR-3 chart + sample-Terraform; explicit no-read property preserved |
| Per-tenant export endpoint shape change | 400 lifted when `isolation_mode='isolated' AND audit.anchor.perTenant=true` for the `?org_id=...&include_anchors=true` combo; CHANGELOG entry locked |
| "Key issued but no first anchor yet" health check | Added in PR-2 |

No deviations from the chosen-option (A) intent — refinements
tighten the design without changing scope.
