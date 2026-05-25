# Audit trail operator guide

> Audience: SOC2 / GDPR compliance teams + SRE operators of AIFactory.
> Compliance frameworks this supports: SOC2 CC7.2 (system monitoring),
> CC6.1 (logical access), GDPR Art. 17 (right to erasure), Art. 5
> (storage limitation).

## What ships in v1.0

| Feature | Where |
| --- | --- |
| Tamper-evident hash chain on every write | `server/services/audit_chain.py` |
| Default retention: 13 months | `server/services/audit_service.py` |
| Streaming JSON (NDJSON) + CSV export | `GET /api/audit/export` |
| External verifier (air-gappable) | `python -m server.audit verify-chain` |
| GDPR Art. 17 erasure | `POST /api/users/{id}/gdpr-erasure` |
| Daily retention job | `python -m server.jobs.audit_retention` |

## Threat model

### What the chain protects against

The hash chain (Epic #26 P5.2) protects against **at-rest tampering
of the audit log database** by an attacker who:

- Has write access to the audit_logs table (DB admin, compromised
  replica, malicious insider, etc.).
- Wants to insert / delete / mutate specific rows to cover their
  tracks.
- CANNOT also re-compute the entire chain forward from the
  modification point.

Any of those mutations breaks `verify_chain`. The break is detected
either at next read or by an out-of-band verifier run (recommended
daily; see "Verification" below).

### What the chain does NOT protect against

- An attacker who has write access to the DB AND can re-compute the
  full chain (= any DB admin). Defense: **signed external anchor**
  — periodically commit the current chain head to an external
  trust anchor (transparency log, blockchain timestamp, signed S3
  object with object-lock, etc.). v1.1 ships a signed-anchor mode
  (Epic #35 v1.1 audit-chain).
- An attacker who modifies the application code to write false
  events. Defense: image signing (Epic #26 P0 cosign) + admission
  policy enforcing the signature.

This limitation is documented in the SOC2 evidence pack (P7).

## Default retention policy

| Action class | retention_until on write |
| --- | --- |
| All | now + 395 days (13 months) |

Per-action policies are a one-line change in
`server.services.audit_service.log_audit_event`. For v1.0 the
uniform 13-month default satisfies SOC2 (12mo minimum + buffer for
auditor lead time).

To change globally for an install: edit the constant and re-deploy.
To support multiple per-action TTLs without a code change: extend
the ConfigMap and read at write-time. (v1.1 task.)

## Streaming export

```bash
# JSON (NDJSON, one row per line):
curl -fsSL -H "Authorization: Bearer $TOKEN" \
  "https://aifactory.example.com/api/audit/export?format=json" \
  -o audit-export.ndjson

# CSV (RFC 4180 with header row):
curl -fsSL -H "Authorization: Bearer $TOKEN" \
  "https://aifactory.example.com/api/audit/export?format=csv&from=2026-01-01" \
  -o audit-export.csv

# Filters: ?org_id=... &from=ISO8601 &to=ISO8601
```

Stable column order — downstream tooling depends on this:

```
id, created_at, action, user_id, org_id, resource_type, resource_id,
ip, details_json, prev_hash, retention_until
```

Adding columns at the end is non-breaking. Reordering is a SemVer
major change.

## Verification

### Periodic in-cluster verification (recommended: daily)

```bash
# Inside a maintenance window with read access to the audit table:
curl -fsSL -H "Authorization: Bearer $TOKEN" \
  https://aifactory.example.com/api/audit/export?format=json \
  | python -m server.audit verify-chain /dev/stdin

# Expected output: "OK: N rows verified" (exit 0)
```

### Air-gapped verification (compliance audit window)

The verifier has zero external dependencies beyond Python stdlib +
the `server.services.audit_chain` module. Operators can:

1. Export the audit log via the API.
2. Copy the export tarball + the AIFactory source to a clean
   machine (e.g. an auditor's laptop).
3. Run:
   ```bash
   PYTHONPATH=apps/web-server python -m server.audit verify-chain audit-export.ndjson
   ```

Exit codes:
- `0` — chain verifies end-to-end.
- `1` — chain verification failed at a specific row (prints index + reason).
- `2` — file read / JSON parse error.

## GDPR right-to-erasure

```bash
# Trigger erasure:
curl -fsSL -X POST -H "Authorization: Bearer $TOKEN" \
  https://aifactory.example.com/api/users/$USER_ID/gdpr-erasure

# Response:
# {
#   "user_id": "<original>",
#   "hashed_user_id": "<sha256[:36]>",
#   "audit_rows_anonymized": N,
#   "email_accounts_deleted": M,
#   "erased_at": "...",
#   "idempotent": false
# }
```

What erasure does:

1. `users.email`, `users.name`, `users.avatar_url` → `NULL`.
2. `users.gdpr_erased_at` set to the current timestamp.
3. Every `audit_logs` row with `user_id = <original>` has its
   `user_id` replaced with `sha256(user_id)[:36]` — irreversible.
4. `details_json` redacted: any key whose name contains
   `email`/`name`/`ip`/`phone`/`address`/`ssn` (case-insensitive)
   has its value replaced with `"<redacted>"`.
5. `email_accounts` rows for the user are hard-deleted (OAuth
   tokens have no legal basis for retention post-erasure).
6. The audit chain is RE-HASHED from the start so `verify_chain`
   still passes — the chain represents the post-erasure state.

Idempotent: re-running on an already-erased user returns
`idempotent: true` with the existing record and does NOT re-hash.

### Limitations

- **Chain re-hashing is O(N).** For 1M-row audit logs the operator-
  triggered erasure walks the entire table. Schedule erasures during
  maintenance windows. v1.1 will optimize by starting from the first
  affected row.
- **Cross-system erasure is the operator's responsibility.** If you
  ship audit events to an external SIEM, GDPR requires you to erase
  there too — the API does NOT call out to external systems.
- **Backups are not erased.** If your DR strategy includes backups
  taken before erasure, those still contain PII. Document the
  retention policy on backups as part of your DPIA (P7 SOC2
  evidence pack).
- **The PII redaction substring list is best-effort.** Operators
  with stricter data-classification policies should review
  `_is_pii_key` in `gdpr.py` and extend the substring set.

## Retention job

```bash
# Manual run:
python -m server.jobs.audit_retention

# Output (example):
# {'as_of': '2026-05-25T19:00:00', 'deleted': 1247, 'remaining': 89321}
```

For Helm-deployed clusters, schedule as a CronJob (template lands in
v1.0.1):

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: aifactory-audit-retention
spec:
  schedule: "0 3 * * *"  # 03:00 UTC daily
  jobTemplate:
    spec:
      template:
        spec:
          containers:
            - name: retention
              image: ghcr.io/dataseeek/aifactory:1.0.0
              command: ["python", "-m", "server.jobs.audit_retention"]
              env:
                - name: DATABASE_URL
                  valueFrom:
                    secretKeyRef: {name: aifactory-db, key: database-url}
          restartPolicy: OnFailure
```

## SOC2 evidence pointers

When the auditor asks "show me your audit trail integrity controls":

1. Reference this document for the threat model + the documented
   v1.1 signed-anchor upgrade path.
2. Show a recent successful `verify-chain` run output (the
   periodic in-cluster verification log).
3. Show the retention job's recent run history (`kubectl get
   jobs -l app=aifactory-audit-retention`).
4. Show one example GDPR erasure record with the audit chain
   verifying post-erasure.

## Related

- [helm-install.md](../deployment/helm-install.md) — chart install runbook.
- [encrypted-secrets-dr.md](encrypted-secrets-dr.md) — DR for the
  secrets layer (audit retention is separate from secrets retention).
- Source: `apps/web-server/server/services/audit_chain.py`,
  `apps/web-server/server/services/gdpr.py`,
  `apps/web-server/server/jobs/audit_retention.py`,
  `apps/web-server/server/audit/__main__.py`.
