# Upgrade Guide

> Audience: platform / SRE teams upgrading AIFactory between versions.
> Status: v1.0 (closing Epic #26).
> Scope: covers v0.x → v1.0 (greenfield migration) and v1.0.x → v1.0.y
> (minor upgrades).

## Version compatibility matrix

| From | To | Method | Estimated downtime |
| --- | --- | --- | --- |
| v0.x (pre-v1.0) | v1.0.0 | One-time migration; see §1 | 5-15 min |
| v1.0.0 | v1.0.x | `helm upgrade` | < 30s rolling |
| v1.0.x | v1.1.0 (future) | TBD (will document in v1.1 release notes) | TBD |

## Pre-upgrade checklist (every upgrade)

- [ ] `helm list` — note the current chart version.
- [ ] `kubectl get deploy aifactory -o jsonpath='{.spec.replicas}'` — capture for rollback verification.
- [ ] `pg_dump` of the entire database. **NOT optional for v0.x → v1.0** (forward-only migration).
- [ ] Backup the verified-good audit chain hash:
      ```bash
      curl -fsSL "https://aifactory/api/audit/export?format=json" \
          | python -m server.audit verify-chain /dev/stdin && \
      curl -fsSL "https://aifactory/api/audit/export?format=json" \
          | tail -1 | jq -r .prev_hash > pre-upgrade-chain-head.txt
      ```
- [ ] Confirm rollback procedure ready (see §3).
- [ ] Maintenance window scheduled + customer notified.

---

## §1. v0.x → v1.0 upgrade

This is a **one-time, forward-only migration**. v0.x had plaintext
credentials in `email_accounts` and `llm_endpoints`; v1.0 encrypts
them via P2's EncryptedString layer.

> ⚠ **CRITICAL**: the encrypted-column migration
> (`c6e3b2d4a8f0_encrypt_credentials`) is **forward-only**. After it
> runs, the plaintext credentials are GONE. The only rollback is
> restoring the pre-migration `pg_dump` — there is no "downgrade"
> path. **Do not skip the backup step.**

### Procedure

```bash
# 1. Pre-flight backup
DB_URL="postgresql://aifactory:...@db-host:5432/aifactory"
pg_dump --format=custom --file=pre-v1.0-backup.dump "$DB_URL"
# Verify the backup is restorable in a separate DB before proceeding:
psql -c 'CREATE DATABASE aifactory_restore_test;'
pg_restore --dbname=aifactory_restore_test pre-v1.0-backup.dump
psql aifactory_restore_test -c 'SELECT count(*) FROM users;'

# 2. Provision KMS root (per the chosen backend's runbook)
# AWS example — see guides/operations/kms-rotation-runbook.md
aws kms create-key --description "aifactory-v1.0 root"

# 3. Pre-seed the database connection + KMS env in the upgrade Job
# (Helm v1.0 chart picks these up automatically)

# 4. Helm upgrade with migrations.autoApply=false (run migration as Job)
helm upgrade aifactory ./charts/aifactory \
    -f values-prod.yaml \
    --set migrations.autoApply=false \
    --wait --timeout=10m

# 5. Run the Alembic migration Job out-of-band so it's auditable
kubectl run aifactory-upgrade-v1.0 \
    --image=$IMAGE \
    --restart=Never \
    --env "DATABASE_URL=$DB_URL" \
    --env "APP_KMS_BACKEND=aws_kms" \
    --env "AWS_KMS_KEY_ID=$CMK_ARN" \
    -- python -m alembic upgrade head
kubectl logs aifactory-upgrade-v1.0
kubectl delete pod aifactory-upgrade-v1.0

# 6. Verify the migration encrypted what it should
psql "$DB_URL" -c "
    SELECT
        COUNT(*) AS total,
        SUM(CASE WHEN octet_length(access_token) > 100 THEN 1 ELSE 0 END) AS likely_encrypted
    FROM email_accounts;
"
# Expected: both counts equal (every row encrypted).

# 7. Restart the app so the new schema is picked up
kubectl rollout restart deploy/aifactory
kubectl rollout status deploy/aifactory --timeout=5m

# 8. Smoke test: existing users can still log in + decrypt their tokens
# (manual — perform a real login on the upgraded instance)
```

### v0.x → v1.0 data flow

```
BEFORE (v0.x)                       AFTER (v1.0)
─────────────                       ────────────
users.email                         users.email
users.password_hash                 users.password_hash
                                    users.oidc_sub          (new — nullable)
                                    users.gdpr_erased_at    (new — nullable)

email_accounts.access_token TEXT    email_accounts.access_token BYTEA
  (plaintext)                         (AES-256-GCM, key wrapped via KMS)

(no audit chain)                    audit_logs.prev_hash  (chained on every write)
                                    audit_logs.retention_until (13mo default)

(no kms_data_keys)                  kms_data_keys table   (per-org data keys)
```

### Common v0.x → v1.0 failure modes

| Symptom | Root cause | Fix |
| --- | --- | --- |
| Migration fails: "KMS_FERNET_KEY env var is not set" | KMS env not wired to the upgrade Job pod | Set the env per the chosen KMS backend's spec (see kms-rotation-runbook.md). |
| Some users can log in, others can't, after upgrade | Migration ran with wrong KMS key — partial encryption | Restore from `pre-v1.0-backup.dump`, re-run with correct KMS. |
| App pod CrashLoopBackOff: "no such column: oidc_sub" | Schema migration didn't run before the new pod booted | `migrations.autoApply=true` for this single upgrade, OR run the Alembic Job before `helm upgrade`. |
| Audit chain doesn't verify post-upgrade | Pre-existing audit_logs rows have NULL prev_hash | Expected — the chain begins from the first row written under v1.0. Verifier accepts NULL prev_hash on the first row as GENESIS. |

---

## §2. v1.0.x → v1.0.y minor upgrades

Standard `helm upgrade` flow. No schema changes between patch versions.

```bash
helm upgrade aifactory ./charts/aifactory \
    -f values-prod.yaml \
    --wait --timeout=5m

# Verify:
kubectl get pods -l app.kubernetes.io/name=aifactory -o wide
kubectl logs deploy/aifactory --tail=20
curl -fsSL https://aifactory/api/health
```

The chart's `RollingUpdate` with `maxSurge=0` is intentional for v1.0:
single-replica + ordered tear-down-then-bring-up. No double-write
window during the version switch.

---

## §3. Rollback procedure

### From v1.0.x to v1.0.(x-1) — patch downgrade

```bash
helm rollback aifactory 0  # one revision back
```

### From v1.0.0 to v0.x — RESTORE FROM BACKUP

This is **not a `helm rollback`** operation because the schema
migration is forward-only. Steps:

```bash
# 1. Scale to 0 replicas (no writes during restore)
kubectl scale deploy/aifactory --replicas=0

# 2. Restore the pre-upgrade pg_dump
psql "$DB_URL" -c 'DROP DATABASE aifactory_temp;'  # if already exists
psql "$DB_URL" -c 'CREATE DATABASE aifactory_temp;'
pg_restore --dbname=aifactory_temp pre-v1.0-backup.dump

# 3. Swap (DDL operation; brief lock):
psql -c 'ALTER DATABASE aifactory RENAME TO aifactory_failed_v1;'
psql -c 'ALTER DATABASE aifactory_temp RENAME TO aifactory;'

# 4. helm rollback to v0.x revision
helm rollback aifactory <revision>

# 5. Scale back up
kubectl scale deploy/aifactory --replicas=1

# 6. Verify users can log in with original credentials
```

**Data loss window**: any audit logs / user actions performed
between the pre-upgrade pg_dump and the restore are LOST. This is
why the maintenance window is critical for v0.x → v1.0.

### Drill verification

Run `scripts/drills/upgrade-in-place.sh --dry-run` before the live
upgrade. The drill rehearses:
1. Seeding a synthetic v0.x DB
2. Running the migration
3. Verifying readback works
4. Rolling back to v0.x by restore

---

## §4. Post-upgrade verification gate

A successful upgrade must pass ALL of:

```bash
# 1. New version reported
helm list -A | grep aifactory  # CHART_VERSION = 1.0.0

# 2. Pod healthy on new image
kubectl get deploy/aifactory -o jsonpath='{.spec.template.spec.containers[0].image}'

# 3. Audit chain re-verifies (catches if anything was tampered during upgrade)
curl -fsSL "https://aifactory/api/audit/export?format=json" | \
    python -m server.audit verify-chain /dev/stdin

# 4. Existing user can log in (manual, NOT automatic)

# 5. Encrypted credential round-trip works
# Trigger an OAuth refresh on any existing email_accounts row + verify
# the token still authenticates against the upstream service.

# 6. Compare chain head against pre-upgrade
NEW_HEAD=$(curl -fsSL "https://aifactory/api/audit/export?format=json" \
    | tail -1 | jq -r .prev_hash)
echo "$NEW_HEAD"  # should match or extend pre-upgrade-chain-head.txt
```

## Reviewer signoff

| Role | Name | Date |
| --- | --- | --- |
| Author | Olaf Krasicki-Freund | 2026-05-25 |
| Walkthrough | _TBD via PR review_ | _TBD_ |
