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

## Evidence drop-path (MinIO)

The evidence-collector CronJob (factory-gitops#74) snapshots what an in-cluster
job can reach into the `factory-evidence` MinIO bucket, and reserves a stable
prefix for evidence the control plane must push itself:

```
factory-evidence/control-plane-push/<source>/...
```

### Access-review export (automated)

The control plane pushes the fleet-wide access-review roster (SOC 2 CC6.2 /
ISO 27001 A.9.2.5) daily. It runs the same export that backs
`GET /api/admin/access-review` — across every non-deleted org — and uploads the
dated NDJSON to:

```
s3://factory-evidence/control-plane-push/access-review/<YYYY-MM-DD>.ndjson
```

Code: `apps/web-server/server/jobs/access_review_evidence_cron.py`. It reuses the
artifact-store MinIO env namespace already on the pods (`S3_ENDPOINT`,
`S3_ACCESS_KEY`, `S3_SECRET_KEY`, `S3_REGION`; bucket override
`EVIDENCE_S3_BUCKET`) and tags each object `role=evidence` so the bucket's
retention lifecycle matches. Idempotent (same-day re-run overwrites) and
fail-safe (a push error logs and is retried on the next tick).

Run as a Kubernetes CronJob (or by hand):

```bash
python -m server.jobs.access_review_evidence_cron
```

### Scan results (CI-pushed)

Trivy / CodeQL scan results are pushed from CI (not the cluster) to the sibling
prefix `control-plane-push/scan-results/`. From a workflow that has the MinIO
credentials as secrets, the push is a single object-copy — either `mc` or the
AWS CLI, both S3-compatible against MinIO:

```bash
# mc (MinIO client)
mc alias set evidence "$S3_ENDPOINT" "$S3_ACCESS_KEY" "$S3_SECRET_KEY"
mc cp --attr "role=evidence" trivy-results.sarif \
  "evidence/factory-evidence/control-plane-push/scan-results/$(date -u +%F)-trivy.sarif"

# aws cli (equivalent)
aws --endpoint-url "$S3_ENDPOINT" s3 cp trivy-results.sarif \
  "s3://factory-evidence/control-plane-push/scan-results/$(date -u +%F)-trivy.sarif"
```

Wiring this into the `codeql.yml` / release Trivy workflow as a final upload
step is a documented follow-up (a few lines per workflow); the access-review
half above is fully automated.

## Retention

Audit log rows are kept for 7 years by default (configurable via `AUDIT_RETENTION_DAYS`). Older rows are summarized into a single "redaction" row that preserves the hash chain but discards the row body.

## Operator runbook

See the [audit-trail runbook](https://github.com/olafkfreund/AIFactory/blob/main/docs-archive/2026-05-26/guides/operations/audit-trail.md) for incident-response procedures.
