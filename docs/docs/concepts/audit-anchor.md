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

## What's not yet supported

- **External anchor publication.** v1.1 stores anchors in the same Postgres as the audit log. A DB admin with the HMAC key can rewrite both. v1.2 will publish anchors to S3 Object Lock / RFC 3161 TSA / Sigstore for genuine third-party untamperedness.
- **Asymmetric signatures.** v1.1's HMAC means the verifier needs the secret. v1.2 with public verification needs RSA/ECDSA signatures via cloud KMS Sign APIs.
- **Per-event signing.** Daily is sufficient for the v1.1 threat model and operator habits ("send me yesterday's audit roll"). Per-event signing adds per-write overhead with no operational benefit at our scale.

## See also

- [Multi-replica deployment](./multi-replica.md) — Redis fan-out (Epic #35 #40).
- [Distributed tracing](./observability-tracing.md) — OpenTelemetry (Epic #35 #42).
- [ISO 27001 evidence](https://github.com/olafkfreund/AIFactory/blob/dev/guides/compliance/iso27001-evidence.md) — Annex A control mapping (lives outside the Docusaurus tree).
- [GitHub issue #43](https://github.com/olafkfreund/AIFactory/issues/43) — original design.
- Design doc in-repo: `docs/plans/2026-05-28-audit-anchor-design.md`.
