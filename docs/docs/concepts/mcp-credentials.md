---
title: MCP credentials — operator setup
sidebar_position: 6
---

# MCP credentials — operator setup

This page is for operators running AIFactory in Kubernetes (or any environment where the standard cloud-CLI credential files aren't already on disk). For the user-facing view of *what* the default MCP servers do, see [Default MCP servers](./mcp-servers).

There are three credential delivery paths. **Prefer cloud-native identity** where you can. Mount Secrets when you can't.

## Path 1: cloud-native identity (preferred)

Each major cloud has a way to give a Kubernetes pod identity *without* a long-lived secret. Use them.

### AWS — IRSA (IAM Roles for Service Accounts)

Annotate the AIFactory ServiceAccount with the IAM role ARN you want pods to assume. The AWS SDK in the MCP subprocess automatically uses the projected token at `AWS_WEB_IDENTITY_TOKEN_FILE`. **Leave `mcpCredentials.providers.aws = false`** — no Secret mount needed.

```yaml
# values.yaml
serviceAccount:
  annotations:
    eks.amazonaws.com/role-arn: arn:aws:iam::123456789012:role/aifactory-readonly
```

The role's trust policy must allow your EKS OIDC provider to assume it:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Federated": "arn:aws:iam::123456789012:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/EXAMPLED539D4633E53DE1B716D3041E"},
    "Action": "sts:AssumeRoleWithWebIdentity",
    "Condition": {"StringEquals": {
      "oidc.eks.us-east-1.amazonaws.com/id/EXAMPLED539D4633E53DE1B716D3041E:sub": "system:serviceaccount:aifactory:aifactory"
    }}
  }]
}
```

Attach `ReadOnlyAccess` (AWS-managed) or a tighter custom policy with only the `Describe*` / `List*` / `Get*` actions you want the agent to use.

### Azure — AKS Pod Identity / Managed Identity

On AKS with workload identity enabled, annotate the ServiceAccount + set the env var that tells the Azure SDK to look for the projected token.

```yaml
# values.yaml
serviceAccount:
  annotations:
    azure.workload.identity/client-id: "00000000-0000-0000-0000-000000000000"

# Tell the Azure MCP server to use Managed Identity
extraEnv:
  - name: AZURE_USE_MSI
    value: "true"
```

Assign the **Reader** role on the subscription / resource group the agent should see. **Leave `mcpCredentials.providers.azure = false`**.

### GCP — Workload Identity Federation

On GKE Autopilot or any cluster with Workload Identity enabled:

```yaml
serviceAccount:
  annotations:
    iam.gke.io/gcp-service-account: aifactory-readonly@PROJECT_ID.iam.gserviceaccount.com
```

Bind the K8s ServiceAccount to the GCP service account via `gcloud iam service-accounts add-iam-policy-binding`. Grant the GCP SA the **Viewer** role at the appropriate scope. **Leave `mcpCredentials.providers.gcp = false`**.

## Path 2: mounted Secret (fallback)

For bare-metal / on-prem K8s where cloud-native identity isn't available, AIFactory mounts credentials from one Kubernetes Secret with well-known keys.

### One-time setup

From a logged-in workstation (run `aws configure`, `az login`, `gcloud auth`, `gh auth login` first so the local files exist):

```bash
kubectl create secret generic aifactory-mcp-credentials \
  --from-file=aws-credentials=$HOME/.aws/credentials \
  --from-file=kubeconfig=$HOME/.kube/config \
  --from-file=gcp-service-account.json=/path/to/sa.json \
  --from-literal=github-token=ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx \
  --from-literal=gitlab-token=glpat-xxxxxxxxxxxxxxxxxxxx \
  --from-literal=gitlab-instance-url=https://gitlab.example.com \
  --from-literal=azure-devops-token=adopat_xxxxxxxxxx \
  --from-literal=azure-devops-org-url=https://dev.azure.com/myorg \
  --from-literal=azure-tenant-id=00000000-0000-0000-0000-000000000000 \
  --from-literal=azure-client-id=00000000-0000-0000-0000-000000000000 \
  --from-literal=azure-client-secret=NOT_A_REAL_SECRET \
  -n aifactory
```

You don't need every key — populate only the providers you actually use. Missing keys for unmounted providers don't cause failures; the chart only references keys when the corresponding `providers.<name>` toggle is `true`.

### Enable in values.yaml

```yaml
mcpCredentials:
  enabled: true
  secretName: aifactory-mcp-credentials
  providers:
    aws: true            # → /home/nonroot/.aws/credentials (0400)
    kubernetes: true     # → /home/nonroot/.kube/config    (0400)
    gcp: true            # → /etc/aifactory/gcp-sa.json    (0400)
    azure: true          # → AZURE_TENANT_ID + CLIENT_ID + SECRET env
    github: true         # → GITHUB_TOKEN env
    gitlab: true         # → GITLAB_TOKEN + GITLAB_INSTANCE_URL env
    azureDevOps: true    # → AZURE_DEVOPS_PERSONAL_ACCESS_TOKEN + ORG_URL env
```

### What gets mounted where

| Provider | Secret key | Pod path / env var | Mode |
|---|---|---|---|
| aws | `aws-credentials` | `/home/nonroot/.aws/credentials` (file) | 0400 |
| kubernetes | `kubeconfig` | `/home/nonroot/.kube/config` (file) | 0400 |
| gcp | `gcp-service-account.json` | `/etc/aifactory/gcp-sa.json` (file) + `GOOGLE_APPLICATION_CREDENTIALS` env points at it | 0400 |
| azure | `azure-tenant-id`, `azure-client-id`, `azure-client-secret` | `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET` env | — |
| github | `github-token` | `GITHUB_TOKEN` env | — |
| gitlab | `gitlab-token` (+ optional `gitlab-instance-url`) | `GITLAB_TOKEN` + `GITLAB_INSTANCE_URL` env | — |
| azureDevOps | `azure-devops-token`, `azure-devops-org-url` | `AZURE_DEVOPS_PERSONAL_ACCESS_TOKEN`, `AZURE_DEVOPS_ORG_SERVICE_URL` env | — |

File mounts use Kubernetes' `subPath` projection — the Secret key lands at exactly the documented path, not in a directory of projected files. `defaultMode: 0400` matches the cloud CLIs' refusal-to-read-loose-perms posture.

## Path 3: operator config file (laptop only)

For dev / single-host deployments where you want a single place to register credentials without populating cloud CLI files:

```bash
chmod 0700 ~/.aifactory
cat > ~/.aifactory/mcp-credentials.json <<EOF
{
  "github": { "tokenEnv": "GITHUB_TOKEN" },
  "aws": { "profile": "aifactory-readonly" },
  "azure": {
    "tenantId": "00000000-0000-0000-0000-000000000000",
    "clientId": "00000000-0000-0000-0000-000000000000",
    "clientSecretEnv": "AZURE_CLIENT_SECRET"
  },
  "kubernetes": { "kubeconfigPath": "/opt/secrets/kubeconfig" },
  "gcp": { "credentialsPath": "/opt/secrets/gcp-sa.json" },
  "gitlab": {
    "tokenEnv": "GITLAB_TOKEN",
    "instanceUrl": "https://gitlab.example.com"
  },
  "azureDevOps": {
    "tokenEnv": "ADO_PAT",
    "orgUrl": "https://dev.azure.com/myorg"
  }
}
EOF
chmod 0600 ~/.aifactory/mcp-credentials.json
```

The file **never holds secrets in plain text** — only references to env vars (`*Env`) or absolute file paths (`*Path`). AIFactory refuses to read it with looser-than-0600 perms.

## Rotation

### Secret-mounted credentials

Update the K8s Secret:

```bash
kubectl create secret generic aifactory-mcp-credentials \
  --from-file=aws-credentials=$HOME/.aws/credentials \
  ... \
  --dry-run=client -o yaml | kubectl apply -f - -n aifactory
```

The pod sees the new content within ~60 seconds (Kubernetes' default Secret refresh window). MCP subprocesses spawn fresh per task, so the next task picks up the rotated value. **No pod restart needed**.

### Token-bearing env-var providers (GitHub / GitLab / ADO / Azure SP)

Same rotation flow — update the Secret, wait for kubelet refresh, next task picks it up.

### Cloud-native identity (IRSA / Workload Identity)

Rotation happens server-side by the cloud provider. AIFactory doesn't see the rotation.

## Troubleshooting

### Verify credentials are being detected

Run `aifactory --mcp-doctor` on the host (laptop) or `kubectl exec` into the pod:

```bash
kubectl exec -n aifactory deploy/aifactory -- aifactory --mcp-doctor
```

The output shows each catalog entry's marker status + credential probe result + a hint command for missing creds.

### "Operator config refused with permissions ..."

```bash
chmod 0600 ~/.aifactory/mcp-credentials.json
```

### Server starts but tool calls return auth errors

The MCP server itself is reporting bad credentials. AIFactory's probe only checks *presence*, not validity. Common causes:

- Expired AWS access key — run `aws sts get-caller-identity` to confirm
- Expired Azure service-principal secret — check Microsoft Entra
- Stale kubeconfig pointing at a deleted cluster
- GitHub PAT scoped too narrowly — fine-grained tokens need explicit grants per repo

The task's agent log shows the MCP-side error verbatim.

### Server doesn't show up at all

`aifactory --mcp-doctor --project-dir <path>` will tell you which of the three checks failed:

- `✗ markers: none of: has_kubernetes` → the project has no `k8s/` / `charts/` / etc. Add `AGENT_MCP_coder_ADD=kubernetes` to `.aifactory/.env` if you want it anyway.
- `✗ creds: none` → see the hint line for the right CLI to run.

### `kubernetes-mcp-server` version warning

AIFactory pins `kubernetes-mcp-server@>=3.6.0` because **CVE-2026-46519** (CVSS 8.8) in earlier versions lets the `--read-only` flag be bypassed at the execution layer. If `mcp-doctor` warns about an older version, your npm cache may be serving a stale package — clear it (`rm -rf ~/.npm/_npx`) and let AIFactory's `npx` re-fetch.

## See also

- [Default MCP servers](./mcp-servers) — user-facing concept page
- [Remote Control credentials](./remote-control) — same Secret-mounting pattern for a different credential file
