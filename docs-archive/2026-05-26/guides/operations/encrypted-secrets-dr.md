# Encrypted-at-rest secrets — disaster recovery

> Audience: platform / SRE teams responding to a credential-layer
> incident in AIFactory. Use this guide when *something is already
> broken* — for the happy-path rotation procedure, see
> [kms-rotation-runbook.md](kms-rotation-runbook.md).
>
> Goal: restore credential decryption capability in the minimum-blast-radius
> way for each failure scenario.

## The five things that can break

| # | Symptom | Severity | Recovery time |
| --- | --- | --- | --- |
| 1 | KMS root key compromised | Critical | Minutes (revoke) + hours (re-wrap) |
| 2 | KMS root key destroyed (no backup) | Catastrophic | Restore from DB backup taken before encryption |
| 3 | `kms_data_keys` row(s) lost or corrupted | High (per affected org) | Restore that row from DB backup |
| 4 | `EncryptedString` column data corrupted | High (per affected row) | Re-fetch credentials from upstream (OAuth, etc.) |
| 5 | Forgot the KMS root password / lost MFA | Critical | KMS-provider-specific recovery |

The rest of this guide handles each in order.

---

## Scenario 1: KMS root key compromised

**Symptom**: A credential-bearing principal had Decrypt permission on
the root and that principal is suspected compromised (laptop stolen,
PAT leaked to a public repo, ex-employee, etc.).

**Risk**: The attacker can call `Decrypt` on the root, then unwrap
every per-org data key, then AES-256-GCM-decrypt every credential in
`email_accounts` and `llm_endpoints`. Assume **all** credentials are
exposed until proven otherwise.

**Recovery (60-minute drill)**:

1. **Revoke the attacker's access** — the same minute you suspect it.
   - AWS: detach the IAM policy or delete the IAM user.
   - Azure: remove their Key Vault role assignment / access-policy entry.
   - GCP: remove their `Cloud KMS CryptoKey Decrypter` binding.
   - Vault: revoke their token (`vault token revoke <accessor>`) and
     any policies that referenced the transit path.
2. **Rotate the KMS root** following [kms-rotation-runbook.md](kms-rotation-runbook.md).
   This re-wraps every `kms_data_keys` row under a new root. The
   attacker's stolen credentials no longer help — they can't read the
   new root.
3. **Force-invalidate the in-process cache** by restarting every
   AIFactory instance. The `DataKeyManager`'s LRU notices the
   `rotated_at` change anyway (poll interval 60s), but a restart
   removes the window entirely.
4. **Rotate every affected upstream credential** (OAuth tokens,
   provider API keys, etc.). The attacker may have copies of the
   plaintext credentials from before revocation. Treat them as
   exposed. The application will re-receive these via the normal
   re-authentication flows.
5. **Document the incident** for compliance/SOC2: who, what was
   accessed (KMS audit log), what was rotated, when. AWS CloudTrail /
   Azure Activity Log / GCP Cloud Audit Logs / Vault audit logs all
   record every Decrypt operation — this is your evidence trail.

**Recovery is NOT**: restoring a database backup. The database is
fine; the access control is broken.

---

## Scenario 2: KMS root key destroyed (no backup)

**Symptom**: An operator accidentally scheduled deletion of the active
CMK / deleted the Vault Transit key / etc., and the grace period
elapsed. AIFactory now reports `InvalidCiphertextException` (AWS) /
`InvalidTag` (fernet) on every credential read.

**Reality check**: **There is no way to recover the plaintext
credentials** without the root key. The wrapping was AEAD; the
ciphertexts are bit-noise without the key. This is an irrecoverable
loss of credential material — only the application data is recoverable.

**Recovery**:

1. **Provision a fresh KMS root** of the same backend type. This will
   become the new active root.
2. **Restore the database from a backup taken BEFORE the original
   encryption migration** (P2.3: `c6e3b2d4a8f0_encrypt_credentials`).
   The pre-migration columns held plaintext credentials; the rotation
   target can then re-encrypt them on the next write.

   > If you don't have such a backup, the credentials are gone.
   > Application data (specs, tasks, projects) is preserved — only
   > the credential-bearing columns must be re-acquired.

3. **Truncate `kms_data_keys`** — the rows there are now meaningless
   (their `wrapped_key` was for the destroyed root).

   ```sql
   TRUNCATE TABLE kms_data_keys;
   ```

   On the next credential write per org, the `DataKeyManager` will
   mint a fresh data key, wrap it under the new root, and insert a
   new row.

4. **Force users to re-authenticate** — OAuth tokens are gone, so
   email integrations and provider API keys need re-provisioning.
   This is the unavoidable user-visible impact.

5. **Add a CMK protection policy** so it can't happen again. AWS:
   `aws kms put-key-policy` with a Deny statement on
   `kms:ScheduleKeyDeletion` for everyone except a break-glass role.
   Equivalent guards on Azure / GCP / Vault.

**Prevention is the only real cure**: keep the KMS root in a
multi-region replica (AWS multi-region keys, GCP multi-region keyrings)
+ never grant `kms:ScheduleKeyDeletion` to humans.

---

## Scenario 3: `kms_data_keys` row(s) lost or corrupted

**Symptom**: The application can read most credentials but some orgs
report `KeyError` or `InvalidTag` on every credential read. The
`kms_data_keys.wrapped_key` bytes for those orgs are missing or
zero-bytes.

**Cause**: Disk corruption, a botched manual DB migration, a partial
restore.

**Recovery (per affected org)**:

1. **Identify affected orgs** via:

   ```sql
   SELECT org_id FROM kms_data_keys
   WHERE wrapped_key IS NULL OR octet_length(wrapped_key) < 16;
   ```

2. **Restore those rows from the last clean DB backup**:

   ```sql
   -- From a psql session attached to the backup snapshot:
   COPY (
     SELECT * FROM kms_data_keys WHERE org_id = '<affected>'
   ) TO STDOUT;

   -- Then INSERT INTO production.
   ```

3. **If no backup**: delete the row and force the affected org's
   users to re-provision their credentials. The `DataKeyManager`
   will mint a fresh data key on next use. **Existing
   `EncryptedString` rows for that org will be unrecoverable** —
   they were encrypted under the lost data key.

   ```sql
   DELETE FROM kms_data_keys WHERE org_id = '<affected>';
   -- Optional: clear the affected credentials so the app re-prompts.
   UPDATE email_accounts SET access_token = NULL, refresh_token = NULL
     WHERE user_id IN (SELECT id FROM users WHERE organization_id = '<affected>');
   ```

---

## Scenario 4: `EncryptedString` column data corrupted

**Symptom**: One specific row in `email_accounts` / `llm_endpoints`
fails to decrypt. Other rows for the same org work fine.

**Cause**: Row-level disk corruption, an aborted UPDATE statement, a
binary-column-truncating tool (e.g. some ORMs or DB migration scripts
that don't handle bytea correctly).

**Recovery**:

1. **Confirm the org's data key is fine** — try decrypting *other*
   rows for the same `user_id`. If they work, the data key is intact;
   only this row is bad.
2. **Restore the row from backup** if available.
3. **Otherwise, mark the credential as expired** and let the
   application's normal re-authentication flow refresh it:

   ```sql
   UPDATE email_accounts
     SET access_token = NULL, refresh_token = NULL, token_expiry = NOW()
     WHERE id = '<row-id>';
   ```

   Next time the user opens the integration, AIFactory's OAuth
   refresh flow kicks in and writes new encrypted tokens.

---

## Scenario 5: Forgot KMS root password / lost MFA

**Symptom**: Operators cannot authenticate to the KMS provider to
manage the root key. The application still works (it has its own
identity), but no rotation or recovery is possible.

**This is a provider-specific access-recovery issue** — not an
AIFactory-specific concern. Follow the provider's recovery
procedure:

- **AWS**: account root user MFA reset via AWS support.
- **Azure**: subscription owner via Azure support; tenant-level recovery
  via M365 admin.
- **GCP**: organization admin escalation; Google support.
- **Vault**: unseal keys (Shamir share recovery — only works if you
  kept the original shares safely distributed).

While that's in progress, the application continues operating under
its own service identity. Don't rotate during a recovery window —
you may not have the access to complete it.

---

## Pre-incident checklist

Run through these before you need them — much easier than during a
3 a.m. incident:

- [ ] `pg_dump` taken **immediately before** P2.3's
  `c6e3b2d4a8f0_encrypt_credentials` migration ran, kept indefinitely
  in cold storage.
- [ ] Continuous `pg_dump` backups (or PITR via cloud-managed PG)
  retained for at least the cloud provider's KMS deletion grace
  period (AWS = 30 days minimum recommended).
- [ ] KMS audit logging enabled and shipped to an immutable store
  (CloudTrail → S3 with object lock / Azure Activity → Storage with
  immutable blob policy / GCP Audit → BigQuery with retention lock /
  Vault audit → syslog with rotation locks).
- [ ] At least two break-glass identities provisioned with KMS
  Decrypt — distributed across regions / individuals so one
  compromise doesn't strand you.
- [ ] Tested the rotation runbook in staging within the last
  90 days. Document the test in your evidence bucket.

---

## Related

- [kms-rotation-runbook.md](kms-rotation-runbook.md) — happy-path rotation.
- `apps/web-server/server/database/alembic/README.md` — forward-only migration warning.
- `apps/web-server/server/crypto/rotation.py` — `rotate_root()` source.
