# KMS root-key rotation runbook

> Audience: platform / SRE teams operating AIFactory with
> encrypted-at-rest secrets enabled (Epic #26, P2). Compliance frameworks
> requiring this procedure: NIST SP 800-57 (key lifecycle), PCI-DSS 3.6.4
> (annual cryptographic key rotation), SOC2 CC6.1 (logical access).
>
> Goal: rotate the KMS root key under which AIFactory's per-org data
> keys are wrapped — without application downtime, without re-encrypting
> any application data, and with an auditable evidence trail.

## Why this guide exists

AIFactory uses **envelope encryption** for credentials at rest:

```
┌──────────────────────┐   wrapped by    ┌────────────────┐
│ per-org data key     │ ───────────────▶│ KMS root key   │
│ (32 bytes, AES-256)  │                 │ (cloud-managed)│
└──────────┬───────────┘                 └────────────────┘
           │
           │ encrypts (AES-256-GCM)
           ▼
┌──────────────────────────────────────────┐
│ EncryptedString columns:                 │
│   email_accounts.access_token            │
│   email_accounts.refresh_token           │
│   llm_endpoints.api_key                  │
│   (any future credential-bearing column) │
└──────────────────────────────────────────┘
```

Rotating the **KMS root** does NOT require touching the data layer.
It only re-wraps the row in `kms_data_keys` for each org. For a tenant
with 100k orgs, that's 100k tiny KMS API calls — not 100k × 50 columns
of full re-encryption.

The plaintext per-org data keys are **preserved** across rotation, so
`EncryptedString` decryption continues working uninterrupted.

## When to rotate

| Trigger | Cadence | Notes |
| --- | --- | --- |
| Routine | Annually | NIST + PCI-DSS minimum |
| Personnel change | Immediately | Departing operator had Encrypt/Decrypt access |
| Suspected compromise | Immediately | Treat as breach until proven otherwise |
| Cloud provider migration | One-time | AWS → GCP, etc. — see "Cross-backend rotation" |

## The rotation window

A **rotation window** is the period during which **both** the OLD root
and the NEW root are usable. During this window:

- The OLD backend must retain `Decrypt` permission (to unwrap existing
  rows).
- The NEW backend must have `Encrypt` permission (to re-wrap rows).
- A mid-rotation crash leaves some rows under OLD and some under NEW
  — both must remain decryptable until the run completes.

Close the rotation window **only after** verifying every row has been
re-wrapped (see "Verification" below).

---

## Prerequisites

- AIFactory deployed with `APP_KMS_BACKEND` set to one of: `fernet`,
  `aws_kms`, `vault_transit`, `azure_kv`, `gcp_kms`.
- A `pg_dump` backup taken **immediately before** rotation. If the
  rotation fails partway through, this is your recovery point.
- Python 3.12 with the web-server's `requirements.txt` installed
  (any environment that can run AIFactory can run the rotation CLI).
- `DATABASE_URL` exported, pointing at the same Postgres the app uses.

---

## Universal procedure

The rotation CLI follows the same five-step pattern for every backend:

1. **Provision** the new KMS root (backend-specific — see below).
2. **Authorize** the running identity on the new root.
3. **Set** the `*_NEW` env var for your backend (table below).
4. **Run** `python -m server.crypto rotate-root --new-kms-key-id <label>`.
5. **Verify**, then **decommission** the old root.

### `*_NEW` env var by backend

| Backend | OLD env (already set) | NEW env (set BEFORE running rotation) |
| --- | --- | --- |
| `fernet` | `KMS_FERNET_KEY` | `KMS_FERNET_KEY_NEW` |
| `aws_kms` | `AWS_KMS_KEY_ID` | `AWS_KMS_KEY_ID_NEW` |
| `vault_transit` | `VAULT_TRANSIT_KEY` | `VAULT_TRANSIT_KEY_NEW` |
| `azure_kv` | `AZURE_KEYVAULT_KEY` | `AZURE_KEYVAULT_KEY_NEW` |
| `gcp_kms` | `GCP_KMS_KEY_NAME` | `GCP_KMS_KEY_NAME_NEW` |

> **Cross-backend rotation (e.g. AWS → GCP) is NOT supported by the
> CLI.** That's a higher-stakes ops migration with audit implications;
> use `rotate_root()` directly from a custom Python script in that
> case. The CLI assumes the OLD and NEW backends are the same type.

### Step 4: running the CLI

```bash
# From a host with DATABASE_URL + OLD env + NEW env all wired:
python -m server.crypto rotate-root --new-kms-key-id "<audit-label>"
```

Example output on success:

```
KMS root rotation complete: rotated=12473 skipped=0 errors=0 duration=84.2s new_kms_key_id='arn:aws:kms:eu-west-1:1234:key/abcd-1234'
```

Exit codes:
- `0` — all rows rotated successfully.
- `1` — at least one row failed; rotation continued for the rest. Re-run
  after addressing the cause (typically: insufficient IAM on the new
  root, or transient KMS rate-limiting).
- `2` — pre-flight failure (missing env var, OLD == NEW). Nothing was
  rotated.

The `--batch-size` flag (default 100) controls rows per DB round-trip.
Smaller = less lock contention; larger = fewer commits.

---

## Backend-specific provisioning

### AWS KMS

```bash
# 1. Provision a new CMK with the operator's preferred key policy.
NEW_KEY_ID=$(aws kms create-key \
    --description "aifactory-root-$(date +%Y%m%d)" \
    --query 'KeyMetadata.KeyId' --output text)

# 2. (Optional) Alias the new key for human-friendly references.
aws kms create-alias \
    --alias-name alias/aifactory-root-new \
    --target-key-id "$NEW_KEY_ID"

# 3. Grant the AIFactory running identity Encrypt + Decrypt on the new
# CMK. This is typically done by adding the identity's IAM role to the
# new CMK's key policy.

# 4. Run the rotation.
export AWS_KMS_KEY_ID_NEW="$NEW_KEY_ID"
python -m server.crypto rotate-root --new-kms-key-id "$NEW_KEY_ID"

# 5. After verification, flip the runtime env to use the new key, then
# schedule the OLD CMK for deletion (7-30 day grace period).
aws kms schedule-key-deletion --key-id "$OLD_KEY_ID" --pending-window-in-days 30
```

### HashiCorp Vault Transit

Vault Transit's native rotation just bumps the in-place key version,
so the standard procedure is slightly different:

```bash
# Option A (recommended): in-place key version bump.
vault write -f transit/keys/aifactory-root/rotate

# Then force re-wrap of all data keys onto the new version:
export VAULT_TRANSIT_KEY_NEW=aifactory-root
python -m server.crypto rotate-root --new-kms-key-id "aifactory-root@v2"

# Option B: rotate to an entirely separate key (e.g. compromise scenario).
vault write -f transit/keys/aifactory-root-2 type=aes256-gcm96
# Grant the identity transit/encrypt/aifactory-root-2 + transit/decrypt/aifactory-root-2
export VAULT_TRANSIT_KEY_NEW=aifactory-root-2
python -m server.crypto rotate-root --new-kms-key-id "aifactory-root-2"
```

### Azure Key Vault

```bash
# 1. Create a new key in the same vault (or a different vault for full
# blast-radius separation).
az keyvault key create \
    --vault-name kv-aifactory \
    --name aifactory-root-new \
    --kty RSA --size 2048 \
    --ops wrapKey unwrapKey

# 2. Grant the application identity wrapKey + unwrapKey on it
# (Key Vault Crypto User role or access-policy entry).
az role assignment create \
    --assignee "$AIFACTORY_PRINCIPAL_ID" \
    --role "Key Vault Crypto User" \
    --scope "/subscriptions/.../keys/aifactory-root-new"

# 3. Run the rotation.
export AZURE_KEYVAULT_KEY_NEW="aifactory-root-new"
python -m server.crypto rotate-root --new-kms-key-id "aifactory-root-new"
```

### GCP Cloud KMS

```bash
# 1. Provision a new CryptoKey in your keyring.
gcloud kms keys create aifactory-root-new \
    --location global \
    --keyring aifactory \
    --purpose encryption

# 2. Grant the application service account the
# Cloud KMS CryptoKey Encrypter/Decrypter role on it.
gcloud kms keys add-iam-policy-binding aifactory-root-new \
    --location global --keyring aifactory \
    --member "serviceAccount:aifactory@my-project.iam.gserviceaccount.com" \
    --role roles/cloudkms.cryptoKeyEncrypterDecrypter

# 3. Run the rotation.
export GCP_KMS_KEY_NAME_NEW="projects/my-project/locations/global/keyRings/aifactory/cryptoKeys/aifactory-root-new"
python -m server.crypto rotate-root --new-kms-key-id "aifactory-root-new"
```

### Fernet (dev / single-tenant only)

```bash
# 1. Generate a fresh URL-safe-base64 32-byte key.
NEW_KEY=$(python -c 'import base64, secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())')

# 2. Run the rotation.
export KMS_FERNET_KEY_NEW="$NEW_KEY"
python -m server.crypto rotate-root --new-kms-key-id "fernet-$(date +%Y%m%d)"

# 3. Persist the new key in your secret store, then flip the runtime
# env var and restart the application.
```

> **Fernet is NOT recommended for production.** It stores the root key
> as an env var, so anyone with shell access to the host can read it.
> Use one of the cloud backends for any deployment with a compliance
> requirement.

---

## Verification

After the CLI reports success, verify the rotation **before** revoking
the OLD root:

### 1. Row count matches

```sql
-- Every kms_data_keys row should have rotated_at > <run-start time>
-- AND kms_key_id = <the new label you passed via --new-kms-key-id>
SELECT
  COUNT(*) AS total,
  SUM(CASE WHEN rotated_at >= '<run-start-utc>' THEN 1 ELSE 0 END) AS rotated,
  SUM(CASE WHEN kms_key_id = '<new-label>' THEN 1 ELSE 0 END) AS new_label
FROM kms_data_keys;
```

All three counts should be equal.

### 2. A live decrypt round-trips

From the application host (or a Python REPL with `DATABASE_URL` and
the **OLD** env var unset so only the NEW backend is reachable):

```python
from server.crypto import get_backend, DataKeyManager
from sqlalchemy import create_engine
import os

engine = create_engine(os.environ["DATABASE_URL"].replace("+asyncpg", ""))
manager = DataKeyManager(sync_engine=engine, backend=get_backend(), kms_key_id="<new-label>")

# Pick any org id from the kms_data_keys table.
data_key = manager.get_or_create_data_key("<some-org-id>")
assert len(data_key) == 32  # raises if the NEW backend can't unwrap
```

### 3. End-to-end smoke

Trigger a real read against an `EncryptedString` column — e.g. fetch
an `EmailAccount` row via the web UI. If the access token decrypts
cleanly, the full envelope chain is intact under the new root.

---

## Decommissioning the OLD root

Only after all three verification steps pass:

1. **Update runtime env**: flip the application's `KMS_FERNET_KEY` /
   `AWS_KMS_KEY_ID` / equivalent to the NEW value. Restart the
   application so the factory picks up the new env.
2. **Revoke OLD permissions**:
   - AWS: `aws kms schedule-key-deletion --pending-window-in-days 30 ...`
     (the 30-day delay is a defensive window — keep the OLD ARN reachable
     in case verification missed a row).
   - Azure: remove the application identity's `wrapKey`/`unwrapKey`
     permission on the OLD key.
   - GCP: remove the `Encrypter/Decrypter` IAM binding on the OLD key.
   - Vault: leave OLD key versions in place initially; archive after
     30 days via `vault write transit/keys/<name>/config min_decryption_version=<new>`.
3. **Record evidence**: capture the CLI's stdout, the verification
   SQL output, and the timestamps. Store in your SOC2 / audit
   evidence bucket — this is the artifact auditors ask for.

---

## Failure modes

| Symptom | Likely cause | Recovery |
| --- | --- | --- |
| CLI exits 2: `OLD and NEW resolved to the same instance` | The `*_NEW` env var wasn't set, or matches OLD verbatim | Set the correct `*_NEW` env var. No rows touched. |
| CLI exits 1 with errors per org | NEW root lacks `Encrypt` permission for the identity | Grant the missing IAM. Re-run — already-rotated rows are skipped automatically. |
| Some rows fail with `InvalidCiphertextException` (AWS) / `InvalidTag` (fernet) | OLD root permission revoked before rotation completed | Restore OLD root permission, re-run. **Do not** restore from backup — rotation is idempotent. |
| Application sees `InvalidTag` after rotation | App is still configured with the OLD env var | Flip the env to NEW. **Do not** roll back rotation — restart the app instead. |
| Mid-rotation database crash | Connection drop, OOM, etc. | Re-run the CLI. Idempotent skip on rotated rows. |

---

## Related

- [Encrypted-secrets disaster recovery](encrypted-secrets-dr.md) — what to do when verification fails.
- `apps/web-server/server/database/alembic/README.md` — forward-only migration warning for `c6e3b2d4a8f0_encrypt_credentials`.
- Source: `apps/web-server/server/crypto/rotation.py`, `apps/web-server/server/crypto/__main__.py`.
