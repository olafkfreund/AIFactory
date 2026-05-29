---
title: Signed audit-chain anchor
sidebar_position: 12
---

# Signed audit-chain anchor (Epic #35 #43)

> *Daily HMAC-signed snapshot of the audit chain head. Closes the v1.0 limitation where a DB admin could silently rewrite the audit log.*

## When you need this

You want the audit-chain anchor when **any** of these apply:

- You're pursuing **ISO 27001** or **SOC2 Type II** and an auditor will ask: *"how do you prove the audit log hasn't been tampered with by an insider with DB write access?"*
- Your DB admin and your compliance officer are different humans, and compliance needs cryptographic evidence (not just policy) that the DBA didn't rewrite history.
- You export the audit log to external storage or auditor tooling and need an **offline-verifiable proof** of integrity.

You **don't** need it for:

- Laptop installs / dev sessions.
- Single-developer pilots where the same human controls the DB and the audit narrative.
- Sub-100-row-per-day audit volumes (the v1.0 chain already detects post-hoc edits if you keep a copy of `prev_hash` off-system).

## What's in scope (and what's not)

| Aspect | v1.1 status |
|--------|-------------|
| HMAC-SHA256 daily anchor signed with KMS-wrapped key | ✅ |
| Versioned signing keys (KMS rotation doesn't invalidate prior anchors) | ✅ |
| Anchors interleaved into NDJSON export | ✅ |
| Offline verifier helper (`verify_anchored_export`) | ✅ |
| Three-tier data classification (public / internal / confidential) | ✅ |
| Classification tampering detected at anchor-verify time | ✅ |
| Access-review export (`/api/admin/access-review`) | ✅ |
| `users.last_login_at` updated on every successful login | ✅ |
| Kubernetes CronJob OR in-process asyncio scheduler | ✅ |
| External anchor publication (S3 WORM / RFC 3161 TSA / Sigstore) | ❌ (v1.2) |
| Asymmetric signatures for public verification | ❌ (v1.2) |
| Per-event signing | ❌ (intentional — daily granularity matches retention) |

## How it works

```
Day N at 00:00 UTC          Day N+1 at 00:00 UTC
       │                            │
       ▼                            ▼
[reads audit_logs.prev_hash    [reads next chain head]
 of last row before midnight]
       │                            │
       ▼                            ▼
[computes outgoing chain head:
 H = compute_hash(last.prev_hash, last_row)]
       │
       ▼
[computes classifications hash:
 cls_h = SHA256(sorted (id, classification) pairs)]
       │
       ▼
[anchor_input = H + "|" + cls_h]
       │
       ▼
[signature = HMAC-SHA256(signing_key, anchor_input)]
       │
       ▼
[insert audit_anchors row]
```

Each daily anchor signs **two things**:

1. **The chain head** — the hash that the next inserted audit row would store as its `prev_hash`. An attacker who tampers with any row + rewrites the chain to look consistent would still produce a different chain head, breaking the anchor signature.
2. **The classifications-window hash** — SHA-256 of every `(id, classification)` pair in the chain so far, sorted by id. An attacker who flips `confidential → public` to leak rows past an export filter would change this hash, breaking the anchor signature.

The chain itself is **untouched** — pre-#43 audit logs keep verifying via the existing `audit_chain.verify_chain` (no migration required).

## Turning it on

The `audit.anchor:` block in `values.yaml`:

```yaml
audit:
  anchor:
    enabled: true
    scheduler: cronjob          # or "in-process" for single-replica
    cron:
      schedule: "0 0 * * *"     # daily at 00:00 UTC
```

On the next pod restart:
1. The web-server's lifespan bootstraps a 32-byte HMAC key, wraps it with your KMS backend, persists to `audit_signing_keys` as version 1.
2. (CronJob mode) Kubernetes schedules the daily anchor job. The first run backfills any missed days since `audit_signing_keys.created_at`.
3. (In-process mode) An asyncio task in the lifespan does the same.

### Scheduler choice

| Mode | When to use |
|------|------------|
| `cronjob` (default) | Production, multi-replica, anywhere with a real Kubernetes scheduler |
| `in-process` | Single-replica dev / staging where a CronJob feels heavy. The task fires on whichever replica wins startup — fine for `replicaCount=1`, race-condition-prone for `>1` |

### Multi-deployment staggering

If you run multiple AIFactory deployments against the same Postgres (one audit log), they MUST use different schedules or one will lose the daily UTC-day unique race. Stagger:

```yaml
# deployment-A
audit.anchor.cron.schedule: "0 0 * * *"      # 00:00 UTC
# deployment-B
audit.anchor.cron.schedule: "5 0 * * *"      # 00:05 UTC
```

The Postgres unique constraint on `DATE(signed_at)` will reject the second one if both fire at the same time.

## Verifying an export

Operators or auditors verify an exported audit log offline:

```python
from server.services.audit_export import verify_anchored_export

with open("audit-export.ndjson", "rb") as f:
    raw = f.read()

# signing_keys is a dict[int, bytes] — one entry per key_version
# that ever signed an anchor in the export. The operator unwraps
# each version's wrapped_key via the KMS backend they control.
signing_keys = {
    1: b"...32 raw bytes...",
    2: b"...post-rotation 32 raw bytes...",
}

result = verify_anchored_export(raw, signing_keys)
if result.ok:
    print(f"✅ verified {result.rows_verified} rows + {result.anchors_verified} anchors")
else:
    for line_idx, reason in result.failures:
        print(f"❌ line {line_idx}: {reason}")
```

A clean verification proves: every row's `prev_hash` correctly chains to the previous row's content, every anchor's signed chain head matches the running hash through that anchor's window, every anchor's signature validates against the recorded `key_version`'s unwrapped HMAC key.

## Trust scope (what this defends + what it doesn't)

| Threat | Defended? |
|--------|-----------|
| DB read-replica replayed forward | ✅ Anchor mismatch |
| Insertion / deletion of audit rows | ✅ Chain break detected |
| Mutation of row content | ✅ Chain break detected |
| Flipping a `confidential` row to `public` to leak past export filter | ✅ Classifications hash mismatch |
| DB admin re-signing entire chain (no HMAC key access) | ✅ Anchor mismatch |
| DB admin who ALSO has the unwrapped HMAC key | ❌ Out of scope. v1.2 external pub (S3 WORM / RFC 3161 TSA / Sigstore) closes this by writing anchors to a target the admin can't rewrite. |

Operationally: keep the KMS-wrapped key Secret separate from DB admin access. If the same human has both, the anchor is policy evidence, not cryptographic proof.

## Failure-safe contract

Same as #40 / #41 / #42: every signing / cron / export path wraps in `try/except`. A broken KMS or DB **never** crashes the web pod. The next daily tick retries any failed anchor; the startup backfill catches up missed days after any multi-day outage.

## Access-review export

Companion endpoint for SOC2 CC6.2 + ISO 27001 A.9.2.5 quarterly access reviews:

```bash
curl -H "Cookie: access_token=<admin-token>" \
  "https://aifactory.example.com/api/admin/access-review?org=<org-id>" \
  > access-review-Q1.ndjson
```

Returns one NDJSON line per current `OrgMember` with email, role, active, `joined_at`, `last_login_at`. Audit log queries on `org.member.add/remove` events provide the membership-change history.

## Per-tenant chains (v1.2 #208)

Available when `audit.anchor.enabled=true` **and** `audit.anchor.perTenant=true` **and** `tenant.isolationEnabled=true`.

### Architecture

```
Organization A (isolation_mode='isolated')
  audit_logs rows: prev_hash chains to GENESIS-T-<org-a-uuid>
  audit_signing_keys: one row with org_id=<org-a-uuid>
  audit_anchors: daily row with org_id=<org-a-uuid>
       |
       └── verifiable independently with org A's key only

Organization B (isolation_mode='isolated')
  audit_logs rows: prev_hash chains to GENESIS-T-<org-b-uuid>
  audit_signing_keys: one row with org_id=<org-b-uuid>
  audit_anchors: daily row with org_id=<org-b-uuid>
       |
       └── verifiable independently with org B's key only

Non-isolated orgs / pre-cutover rows
  audit_logs: shared chain (GENESIS sentinel, unchanged from v1.1)
  audit_anchors: org_id=NULL (shared deployment anchor)
```

### Three-regime backward-compatibility table

| Org status | Chain mode | Anchor |
|---|---|---|
| Pre-v1.2 (no `tenant_states` row OR `isolation_mode='shared'`) | Shared | Shared deployment anchor (v1.1 unchanged) |
| Post-v1.2, `isolation_mode='isolated'`, cutover at time T | Pre-T rows: shared; post-T rows: per-tenant | Pre-T: shared anchor; post-T: per-tenant anchor |
| `isolation_mode='deleted'` (org soft-deleted) | Per-tenant chain sealed; no new rows | Per-tenant anchor stops at seal time |

### Operator opt-in

```yaml
# charts/aifactory/values.yaml
audit:
  anchor:
    enabled: true       # must be true
    perTenant: true     # enables per-tenant chains
tenant:
  isolationEnabled: true  # required (keys live in Vault paths provisioned by isolation)
```

### Auditor handover workflow

```bash
# 1. Export tenant's rows + anchors:
aifactory audit export --org-id <uuid> --format ndjson --include-anchors > tenant-export.ndjson

# 2. Retrieve wrapped key from Vault:
vault kv get -format=json aifactory/orgs/<uuid>/anchor-key-wrapped > wrapped-key.json

# 3. Unwrap via KMS (one-shot, KMS-audited):
aifactory kms unwrap --backend aws-kms < wrapped-key.json > raw-key.bin

# 4. Verify offline (no DB or KMS access required):
python -m server.audit verify-anchor --org-id <uuid> --export tenant-export.ndjson --key raw-key.bin
PASS: 12,438 rows + 31 anchors verified against tenant chain
```

The KMS unwrap step is the only one requiring operator credentials. Each unwrap is KMS-audited and logged as `audit.handover.tenant-key.unwrap` at `classification='confidential'`.

### ISO 27001 controls enabled by per-tenant chains

- **A.12.4.2** — Tenant-level log integrity protection (v1.1 covered deployment-level only).
- **A.12.4.3** — Operator privilege scope is separated from tenant verification scope.
- **A.18.1.3** — Each tenant holds their own evidence for their ISMS audit.
- **A.18.2.2** — Tenant's auditor can independently attest compliance without seeing other tenants' data.

### What's preserved on org delete

When an org is soft-deleted (`Organization.deleted_at` set), the audit chain rows, `tenant_audit_state` row, and `audit_signing_keys` row all stay as legal-hold artefacts. The per-tenant chain is sealed (`lifecycle='sealed'`); no new rows are appended. The Vault copy of the wrapped key is removed at day-30 tear-down so no further handover is possible, but the DB-side key row stays so historical anchors remain verifiable.

## What's not yet supported

- **External anchor publication.** v1.2 stores anchors in the same Postgres as the audit log. A DB admin with the tenant's HMAC key can rewrite that tenant's chain. v1.3 will publish per-tenant anchors to S3 Object Lock / RFC 3161 TSA / Sigstore for genuine third-party untamperedness.
- **Asymmetric signatures.** v1.2's HMAC means the verifier needs the secret. v1.3 with public verification needs RSA/ECDSA signatures via cloud KMS Sign APIs.
- **Per-event signing.** Daily is sufficient for the v1.2 threat model and operator habits. Per-event signing adds per-write overhead with no operational benefit at our scale.
- **Migration of pre-v1.2 rows to per-tenant chains.** Pre-v1.2 rows participate in the shared chain. Rewriting them into per-tenant chains requires recomputing every `prev_hash` — a destructive one-time operation. Operators wanting a clean per-tenant chain from row 1 must provision a fresh deployment.

## See also

- [Tenant isolation](./tenant-isolation.md) — per-tenant K8s namespaces + Vault paths. Per-tenant audit chains integrate here.
- [Multi-replica deployment](./multi-replica.md) — Redis fan-out (Epic #35 #40).
- [Distributed tracing](./observability-tracing.md) — OpenTelemetry (Epic #35 #42).
- [ISO 27001 evidence](https://github.com/olafkfreund/AIFactory/blob/dev/guides/compliance/iso27001-evidence.md) — Annex A control mapping (lives outside the Docusaurus tree).
- [GitHub issue #43](https://github.com/olafkfreund/AIFactory/issues/43) — v1.1 design.
- [GitHub issue #208](https://github.com/olafkfreund/AIFactory/issues/208) — v1.2 per-tenant design.
- Design docs: `docs/plans/2026-05-28-audit-anchor-design.md` (v1.1), `docs/plans/2026-05-29-per-tenant-audit-anchor-design.md` (v1.2).
