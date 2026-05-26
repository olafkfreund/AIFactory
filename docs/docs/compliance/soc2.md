---
title: SOC 2
sidebar_position: 1
---

# SOC 2 Evidence

AIFactory's enterprise build ships with the controls and evidence trail needed for SOC 2 Type II.

## Controls implemented

| Control | Implementation | Evidence |
|---|---|---|
| **Access control** | OIDC SSO (Keycloak / Okta / Azure AD); per-org role scopes | `apps/web-server/server/auth.py` |
| **Encryption in transit** | TLS-terminated at the ingress; mandated by the Helm chart's NetworkPolicy | `charts/aifactory/templates/networkpolicy.yaml` |
| **Encryption at rest** | All sensitive columns (API keys, OAuth tokens) wrapped in `EncryptedString` SQLAlchemy type backed by KMS (AWS / Azure / GCP / Vault Transit) | `apps/web-server/server/database/encrypted.py` |
| **Audit logging** | Hash-chained audit log table. Each row stores `prev_hash`; tampering breaks the chain. | `apps/web-server/server/database/audit_log.py` |
| **Key rotation** | KMS key rotation runbook. Encrypted columns re-wrap on rotation without downtime. | [Operations: KMS rotation](https://github.com/olafkfreund/AIFactory/blob/main/docs-archive/2026-05-26/guides/operations/kms-rotation-runbook.md) |
| **Backup & DR** | Postgres + WAL archiving; chart configures `pg-backup` sidecar; documented recovery RTO 4h / RPO 1h | [DR runbook](https://github.com/olafkfreund/AIFactory/blob/main/docs-archive/2026-05-26/guides/deployment/runbook.md) |
| **Vulnerability management** | Docker images built distroless; cosign-signed; Syft SBOM published per release | `.github/workflows/release.yml` |

## Evidence catalog

The compliance audit (`docs-archive/2026-05-26/guides/COMPLIANCE_AUDIT_2026-05.md`) maps each SOC 2 Common Criterion to:

- The code or config that implements it
- The test that verifies it
- The runbook that operates it

## Audit log export

For data-subject-access requests, export the audit log:

```bash
curl -H "Authorization: Bearer $TOKEN" \
  "http://aifactory.example.com/api/orgs/$ORG/audit-logs?format=csv&since=2026-01-01" \
  > audit-log.csv
```

The export verifies the hash chain on the way out — a non-zero exit code means an integrity violation was detected, and the export aborts before any data leaks.

## Retention

Audit log rows are kept for 7 years by default (configurable via `AUDIT_RETENTION_DAYS`). Older rows are summarized into a single "redaction" row that preserves the hash chain but discards the row body.

## Operator runbook

See the [audit-trail runbook](https://github.com/olafkfreund/AIFactory/blob/main/docs-archive/2026-05-26/guides/operations/audit-trail.md) for incident-response procedures.
