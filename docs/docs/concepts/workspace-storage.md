---
title: Durable workspace storage (S3)
sidebar_position: 11
---

# Durable workspace storage

> *Snapshot project workspaces to S3-compatible storage at task phase boundaries. Combined with [Redis pub/sub](./multi-replica), this is what makes `replicas > 1` survive pod death without losing in-flight task state. Opt-in.*

AIFactory's workspace storage today is a local-disk PVC: when a project is cloned, it lands at `~/.aifactory/workspaces/<repo>/` (laptop) or `/var/lib/aifactory/workspaces/<repo>/` (K8s). That works fine for single-replica and single-node setups — if the pod restarts on the same node, the PVC re-mounts and everything's still there.

The gap shows up in two places:

1. **Multi-replica.** Pod A clones a project; pod B serves the next request for that project. Without shared storage, pod B has no workspace. (A RWX volume helps but isn't always available — EFS, Azure Files, CephFS only.)
2. **Node death.** PVC is gone. All project state, all in-flight task progress, lost.

S3-compatible durable storage closes both. AIFactory snapshots each project's workspace dir to a configurable bucket at task phase transitions (`coding` → `review_pending` → `completed` / `failed`), and any pod that subsequently needs that workspace and doesn't have it locally restores from the snapshot. The PVC becomes a hot cache; S3 is the durable source of truth.

Disabled by default — laptop installs and single-replica K8s pilots have zero new infra dep.

## When you need this

- You're running AIFactory on K8s with `replicas > 1` and want cross-replica workspace access.
- You can't (or won't) provision RWX persistent storage and want a Helm-installable alternative.
- You want PVC-loss resilience (node failure shouldn't lose project state).
- You're going to deploy multi-tenant later — per-tenant S3 prefixes give you isolation at the storage layer without a per-tenant PVC per tenant.

You **don't** need it for:

- Laptop installs (default behavior unchanged).
- Single-replica K8s where PVC durability is sufficient.
- Single-node deployments where node death = whole cluster death anyway.

## Architecture in 3 bullets

- **What gets snapshotted:** the whole project workspace dir (cloned repo + all nested `.aifactory/specs/*/` + `.aifactory/worktrees/tasks/*/`). One snapshot per project — covers every task that lives under it.
- **When it fires:** at task phase transitions (`coding`, `review_pending`, `completed`, `failed`). ~3-4 uploads per task. Phase boundaries are when state is most coherent (between agent runs).
- **When it restores:** lazily on first access. A pod that loads a project record whose local path doesn't exist downloads the snapshot before returning. Hot path (workspace already present locally) does zero S3 work.

For the full design rationale (why snapshot instead of CSI mount, why per-project instead of per-task, etc.), see `docs/plans/2026-05-28-s3-workspaces-design.md`.

## Enable

### 1. Provision the bucket

```bash
# AWS
aws s3 mb s3://my-aifactory-prod
aws iam attach-role-policy --role-name aifactory-irsa-role \
  --policy-arn arn:aws:iam::aws:policy/AmazonS3FullAccess
# (or a tighter prefix-bounded policy — see "IAM scoping" below)

# MinIO
mc mb local/aifactory-prod
mc admin user add local aifactory <secret>
mc admin policy attach local readwrite --user aifactory

# GCS
gsutil mb gs://my-aifactory-prod

# Azure Blob
az storage container create --name aifactory --account-name myaccount
```

### 2. Set credentials (production path: IRSA / workload identity)

For EKS + IRSA:

```bash
eksctl create iamserviceaccount \
  --cluster=my-cluster \
  --namespace=aifactory \
  --name=aifactory \
  --attach-policy-arn=arn:aws:iam::123:policy/AIFactoryS3Workspaces \
  --override-existing-serviceaccounts \
  --approve
```

For static-creds (less-recommended; works on any cluster):

```bash
kubectl create secret generic aifactory-s3-creds \
  --from-literal=AWS_ACCESS_KEY_ID=AKIA... \
  --from-literal=AWS_SECRET_ACCESS_KEY=... \
  -n aifactory
```

### 3. Flip the chart toggle

```yaml
# values.yaml — IRSA path
workspaces:
  storage:
    enabled: true
    uriBase: "s3://my-aifactory-prod/workspaces"
    aws:
      useInstanceRole: true  # IRSA / instance-role on EKS / EC2

# OR Secret path
workspaces:
  storage:
    enabled: true
    uriBase: "s3://my-aifactory-prod/workspaces"
    aws:
      credentialsSecretName: "aifactory-s3-creds"
```

```yaml
# MinIO
workspaces:
  storage:
    enabled: true
    uriBase: "s3://my-minio-bucket/workspaces"
    aws:
      credentialsSecretName: "minio-creds"
      endpointUrl: "http://minio.minio.svc:9000"
      addressingStyle: "path"  # MinIO needs path-style
```

`helm upgrade` and the next pod rollout will land with `WORKSPACE_S3_URI_BASE` and the AWS-style envs injected. Boot log shows:

```
workspace_store.upload_project: snapshotted /var/lib/aifactory/workspaces/my-repo (1843 files, 234567890 bytes) to s3://my-aifactory-prod/workspaces/default/<project-id>
```

(The `default` org segment is a placeholder until Epic #36 Tenant Isolation populates real `org_id` on each project.)

## Azure Blob / GCS

These backends work via fsspec's `adlfs` and `gcsfs` packages but aren't first-class in v1.1's chart. To use them:

1. Install the extra dep in your image overlay: `pip install adlfs` or `pip install gcsfs`.
2. Set `uriBase: "azure://container/aifactory"` or `uriBase: "gs://my-bucket/workspaces"`.
3. Set the SDK's native env vars via `extraEnv` on the chart:
   - **Azure:** `AZURE_STORAGE_CONNECTION_STRING` or `AZURE_CLIENT_ID` + workload identity
   - **GCS:** mount a service-account JSON to a path + set `GOOGLE_APPLICATION_CREDENTIALS=/path/to/sa.json`, or use Workload Identity Federation

We don't ship typed chart blocks for either until a real pilot exercises them. PRs welcome.

## IAM scoping (least privilege)

The IRSA role / static-creds user needs only:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"],
    "Resource": [
      "arn:aws:s3:::my-aifactory-prod",
      "arn:aws:s3:::my-aifactory-prod/workspaces/*"
    ]
  }]
}
```

Per-tenant isolation lands properly with Epic #36 — for now everything goes under one `default` org segment.

## Failure modes

### S3 unreachable mid-task

The store is failure-safe by design. A failed upload logs a WARNING and the task continues running. Next phase-transition's upload attempt retries automatically — you don't lose the entire task. Cross-replica restore is a hot path so the user impact is "task takes a bit longer to start on the next pod" rather than "task is lost".

### Half-written snapshot

The store writes `_manifest.json` LAST. A download that sees a missing manifest treats the snapshot as incomplete and falls back to a fresh `git clone` from the project's remote. No half-restored worktree ever reaches an agent.

### Credential rotation

Update the Secret (or rotate the IRSA role), restart pods. The fsspec client gets re-instantiated on next access.

### S3 cost runaway

Snapshots fire at phase boundaries, not continuously. Typical: 3-4 PUTs of the entire workspace per task. If a project workspace is 500 MB, a task with 4 phase transitions writes 2 GB. S3 PUT pricing is `$0.005 / 1000 requests` so per-task PUT cost is negligible; per-task storage cost is bucketed by S3 lifecycle rules.

### Retention

The store doesn't manage retention. Configure S3 bucket-level lifecycle rules to expire snapshots after N days. Example (AWS):

```bash
aws s3api put-bucket-lifecycle-configuration --bucket my-aifactory-prod \
  --lifecycle-configuration file://lifecycle.json
```

Where `lifecycle.json` has a rule like "delete objects under `workspaces/` older than 30 days".

## What this does NOT do

- **Restore agent processes on pod death.** Workspaces survive; in-flight agent runs do not. After a pod dies, the user re-triggers the affected task; it picks up from the last phase-boundary snapshot.
- **Bypass audit logging.** Snapshot uploads happen alongside, not instead of, the regular `AuditLog` writes for task state changes.
- **Replace your backup story.** S3 versioning + cross-region replication + lifecycle rules are operator-managed.

## Related

- Epic [#35](https://github.com/olafkfreund/AIFactory/issues/35) — Enterprise v1.1
- Issue [#40](https://github.com/olafkfreund/AIFactory/issues/40) — original two-half issue
- [Multi-replica deployment](./multi-replica) — companion v1.1 spec (Redis pub/sub) you'll want alongside this
- Design doc — `docs/plans/2026-05-28-s3-workspaces-design.md`
- Cross-ref Epic [#36](https://github.com/olafkfreund/AIFactory/issues/36) — Tenant Isolation Mode (per-tenant `org_id` populates the storage prefix when it lands)
