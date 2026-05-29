# AIFactory deployment runbook

> Audience: Platform / SRE operators installing AIFactory for the first time onto a Kubernetes cluster.
> Scope: Helm-based self-hosted install of v1.1, Helm chart at `charts/aifactory/`.
> Companion docs: [`upgrade.md`](./upgrade.md), [`../operations/image-mirroring.md`](../operations/image-mirroring.md), [`../security/threat-model.md`](../security/threat-model.md), [`../compliance/soc2-evidence.md`](../compliance/soc2-evidence.md).
> Verified install paths: EKS, AKS, GKE, vanilla kubeadm + MetalLB. Each path has been smoke-tested against the v1.0.0 release.

## Reading guide

- Start with **Pre-flight checklist** — most failed installs fail because a CNI / KMS / Vault precondition was not met.
- Pick the **cluster-specific section** for your environment.
- Walk through the **install** then the **Verification gate** for the relevant section. Do not skip the verification.
- If something breaks, jump to **Troubleshooting**.

The runbook tries to be a recipe — copy-paste each command, swap in your values, and you should have a working install. Inline rationale is included for the non-obvious steps.

---

## Pre-flight checklist

The following MUST be true before you run `helm install`. The Helm chart's pre-install hook (`templates/pre-install-cni-probe.yaml`) hard-fails the install if CNI requirements are not met, so you will see a clear error at install time — but it is faster to verify these now.

| Precondition                                                                                  | How to verify                                                                                | Why it matters                                                                       |
| --------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------ |
| Kubernetes 1.27+                                                                              | `kubectl version --short`                                                                    | NetworkPolicy v1.27 features used by Helm chart.                                     |
| CNI plugin with NetworkPolicy support — Calico, Cilium, or vendor equivalent                  | `kubectl get crd | grep -E 'cilium|calico'`                                                  | Tenant isolation needs network policies; pre-install hook hard-fails otherwise.       |
| Calico FQDN policy (beta) OR Cilium CiliumNetworkPolicy CRD                                   | `kubectl api-resources | grep -E 'fqdn|ciliumnetworkpolicy'`                                | Only if you enable `tenant.isolationEnabled=true`.                                   |
| Cluster-scoped admin kubeconfig                                                               | `kubectl auth can-i '*' '*' --all-namespaces`                                                 | Helm needs to create namespace + CRDs.                                               |
| Helm 3.12+                                                                                    | `helm version --short`                                                                       | Chart uses Helm v3 lookup functions.                                                 |
| Postgres 15+ reachable from cluster                                                           | Reachable from a debug pod via `psql`                                                        | Stores all persistent state.                                                         |
| KMS backend chosen + AppRole / IAM / Service Principal pre-created                            | `aws kms list-keys` / `az keyvault key list` / `gcloud kms keys list` / `vault read transit/keys/aifactory` | At-rest encryption uses KMS-wrapped DEKs.                                            |
| Vault `aifactory-reconciler` AppRole pre-created (only if `tenant.isolationEnabled=true`)     | `vault read auth/approle/role/aifactory-reconciler`                                          | Reconciler writes per-tenant Vault paths; needs an AppRole with MANAGE-but-not-READ. |
| OIDC IdP (Okta / Azure AD / Google / Keycloak / Auth0) with a confidential client configured  | IdP admin console                                                                            | SSO is the only login path (no local passwords).                                     |
| Container registry credentials in a `Secret` if pulling from a private mirror                  | `kubectl get secret regcred -n aifactory`                                                    | See [`../operations/image-mirroring.md`](../operations/image-mirroring.md) for mirroring.   |

### KMS backend choice

| Backend                                                                                      | Use when                                  | Helm value                                                                              |
| -------------------------------------------------------------------------------------------- | ----------------------------------------- | --------------------------------------------------------------------------------------- |
| Fernet (local key in K8s Secret)                                                              | Dev / sandbox only                        | `crypto.backend=fernet`                                                                 |
| AWS KMS                                                                                       | Cluster on EKS or AWS-managed Postgres    | `crypto.backend=awskms`, `crypto.awskms.keyId=arn:aws:kms:...`                          |
| Vault Transit                                                                                 | On-prem; want crypto-as-a-service          | `crypto.backend=vault`, `crypto.vault.address=...`, `crypto.vault.transitKey=aifactory`  |
| Azure Key Vault                                                                              | Cluster on AKS                            | `crypto.backend=azurekv`, `crypto.azurekv.vaultUrl=...`, `crypto.azurekv.keyName=aifactory` |
| GCP KMS                                                                                       | Cluster on GKE                            | `crypto.backend=gcpkms`, `crypto.gcpkms.keyName=projects/.../locations/.../keyRings/.../cryptoKeys/...` |

### Pre-create the Vault reconciler AppRole

Only if you intend to enable Tenant Isolation Mode. **Never** use a root token; this is a documented anti-pattern.

```hcl
# Vault policy: aifactory-reconciler
path "sys/policies/acl/aifactory-tenant-*" {
  capabilities = ["create", "read", "update", "delete", "list"]
}
path "auth/kubernetes/role/aifactory-tenant-*" {
  capabilities = ["create", "read", "update", "delete", "list"]
}
# Note: NO `data/aifactory/orgs/*` read — reconciler must not see tenant secrets.
```

```bash
vault policy write aifactory-reconciler /tmp/aifactory-reconciler.hcl
vault auth enable approle
vault write auth/approle/role/aifactory-reconciler \
    token_policies=aifactory-reconciler \
    token_ttl=1h token_max_ttl=4h
vault read -format=json auth/approle/role/aifactory-reconciler/role-id
vault write -format=json -f auth/approle/role/aifactory-reconciler/secret-id
```

Capture the role-id + secret-id into a Kubernetes Secret named `aifactory-vault-approle` in the `aifactory` namespace.

---

## Path A — EKS (AWS)

### Cluster prerequisites

- EKS 1.27+ with the VPC-CNI replaced by Calico or Cilium (VPC-CNI's NetworkPolicy support is too new for the tenant-isolation flow in v1.1).
- Managed RDS Postgres 15+ in the same VPC.
- IAM OIDC provider associated with the cluster (`eksctl utils associate-iam-oidc-provider`).
- AWS KMS key created in the cluster region with an IAM policy that grants `kms:Encrypt`/`Decrypt`/`GenerateDataKey` to the operator IAM role.

### Install

```bash
# 1. Create namespace.
kubectl create namespace aifactory

# 2. Create the IRSA (IAM-Roles-for-ServiceAccounts) binding for the web pod.
eksctl create iamserviceaccount \
    --cluster=<your-cluster> --region=us-east-1 \
    --namespace=aifactory --name=aifactory-web \
    --attach-policy-arn=arn:aws:iam::<account>:policy/AIFactoryKMSAccess \
    --approve

# 3. Create the Postgres password Secret.
kubectl -n aifactory create secret generic aifactory-db \
    --from-literal=DATABASE_URL='postgresql://aifactory:<pw>@<rds-host>:5432/aifactory'

# 4. Render values + install.
helm install aifactory ./charts/aifactory \
    -n aifactory \
    --set image.registry=ghcr.io \
    --set image.tag=v1.0.0 \
    --set crypto.backend=awskms \
    --set crypto.awskms.keyId=arn:aws:kms:us-east-1:<acct>:key/<id> \
    --set serviceAccount.create=false \
    --set serviceAccount.name=aifactory-web \
    --set oidc.issuer=https://<idp> \
    --set oidc.clientId=<id> \
    --set ingress.host=aifactory.example.com
```

### Verification gate (EKS)

```bash
# All pods Ready.
kubectl -n aifactory get pods
# Health endpoint returns {"status":"ok"}.
kubectl -n aifactory port-forward svc/aifactory-web 8080:80 &
curl -s http://localhost:8080/api/health | jq .
# Metrics endpoint reachable (token-gated if METRICS_SCRAPE_TOKEN set).
curl -s -H "Authorization: Bearer $METRICS_SCRAPE_TOKEN" \
    http://localhost:8080/metrics | head -20
```

Smoke-test SSO + create a task — see **End-to-end smoke test** below.

---

## Path B — AKS (Azure)

### Cluster prerequisites

- AKS 1.27+ with the Azure CNI replaced by Cilium (AKS supports Cilium as a Network Dataplane option since 1.27).
- Azure Database for Postgres 15+ in the same VNet.
- Azure Key Vault with a key created; AKS pods authenticate via Workload Identity (recommended) or AAD Pod Identity (deprecated).
- Workload Identity OIDC issuer URL captured (`az aks show -g <rg> -n <cluster> --query oidcIssuerProfile`).

### Install

```bash
kubectl create namespace aifactory

# Workload Identity binding for the web ServiceAccount.
az identity create -g <rg> -n aifactory-web-mi
az identity federated-credential create --identity-name aifactory-web-mi \
    --resource-group <rg> \
    --issuer <oidc-issuer> \
    --subject system:serviceaccount:aifactory:aifactory-web \
    --audiences api://AzureADTokenExchange

# Grant the managed identity Key Vault crypto-user.
az role assignment create \
    --assignee <mi-client-id> \
    --role "Key Vault Crypto User" \
    --scope $(az keyvault show -n <kv> --query id -o tsv)

# Postgres Secret.
kubectl -n aifactory create secret generic aifactory-db \
    --from-literal=DATABASE_URL='postgresql://aifactory:<pw>@<pg-host>:5432/aifactory?sslmode=require'

# Install.
helm install aifactory ./charts/aifactory -n aifactory \
    --set image.registry=ghcr.io --set image.tag=v1.0.0 \
    --set crypto.backend=azurekv \
    --set crypto.azurekv.vaultUrl=https://<kv>.vault.azure.net \
    --set crypto.azurekv.keyName=aifactory \
    --set serviceAccount.annotations."azure\.workload\.identity/client-id"=<mi-client-id> \
    --set oidc.issuer=https://login.microsoftonline.com/<tenant>/v2.0 \
    --set oidc.clientId=<id> \
    --set ingress.host=aifactory.example.com
```

### Verification gate (AKS)

```bash
kubectl -n aifactory get pods
kubectl -n aifactory logs deploy/aifactory-web | grep -i "kms"  # expect successful Key Vault handshake
kubectl -n aifactory port-forward svc/aifactory-web 8080:80 &
curl -s http://localhost:8080/api/health | jq .
```

---

## Path C — GKE (Google Cloud)

### Cluster prerequisites

- GKE 1.27+ with Dataplane V2 (which is Cilium under the hood) and NetworkPolicy enforcement enabled.
- Cloud SQL for Postgres 15+ in the same VPC; private IP recommended.
- Cloud KMS key created; Workload Identity binding for the GSA.

### Install

```bash
kubectl create namespace aifactory

# Workload Identity binding.
gcloud iam service-accounts create aifactory-web --project <project>
gcloud kms keys add-iam-policy-binding aifactory \
    --keyring=aifactory --location=us \
    --member="serviceAccount:aifactory-web@<project>.iam.gserviceaccount.com" \
    --role=roles/cloudkms.cryptoKeyEncrypterDecrypter

gcloud iam service-accounts add-iam-policy-binding \
    aifactory-web@<project>.iam.gserviceaccount.com \
    --member="serviceAccount:<project>.svc.id.goog[aifactory/aifactory-web]" \
    --role=roles/iam.workloadIdentityUser

# Postgres Secret.
kubectl -n aifactory create secret generic aifactory-db \
    --from-literal=DATABASE_URL='postgresql://aifactory:<pw>@<sql-private-ip>:5432/aifactory'

# Install.
helm install aifactory ./charts/aifactory -n aifactory \
    --set image.registry=ghcr.io --set image.tag=v1.0.0 \
    --set crypto.backend=gcpkms \
    --set crypto.gcpkms.keyName=projects/<p>/locations/us/keyRings/aifactory/cryptoKeys/aifactory \
    --set serviceAccount.annotations."iam\.gke\.io/gcp-service-account"=aifactory-web@<p>.iam.gserviceaccount.com \
    --set oidc.issuer=https://accounts.google.com \
    --set oidc.clientId=<id>.apps.googleusercontent.com \
    --set ingress.host=aifactory.example.com
```

### Verification gate (GKE)

```bash
kubectl -n aifactory get pods
kubectl -n aifactory logs deploy/aifactory-web | grep -i "kms"  # expect successful GCP KMS handshake
kubectl -n aifactory port-forward svc/aifactory-web 8080:80 &
curl -s http://localhost:8080/api/health | jq .
```

---

## Path D — Vanilla Kubernetes + Vault

For on-prem clusters (kubeadm, k3s, RKE2) where you want Vault as the KMS backend.

### Cluster prerequisites

- Kubernetes 1.27+ with Calico or Cilium.
- Vault 1.13+ reachable from the cluster; Transit secrets engine enabled at `transit/`; an encryption key `aifactory` created.
- Postgres 15+ on a host reachable from the cluster.
- `kubectl` admin kubeconfig.
- (Optional but recommended) `aifactory-reconciler` Vault AppRole pre-created per the pre-flight checklist.

### Install

```bash
kubectl create namespace aifactory

# Vault token Secret for the web pod (or AppRole role-id / secret-id pair).
kubectl -n aifactory create secret generic aifactory-vault \
    --from-literal=VAULT_TOKEN='<token-with-transit-encrypt-decrypt-on-aifactory-key>' \
    --from-literal=VAULT_ADDR='https://vault.example.com:8200'

# Postgres Secret.
kubectl -n aifactory create secret generic aifactory-db \
    --from-literal=DATABASE_URL='postgresql://aifactory:<pw>@<pg-host>:5432/aifactory'

helm install aifactory ./charts/aifactory -n aifactory \
    --set image.registry=ghcr.io --set image.tag=v1.0.0 \
    --set crypto.backend=vault \
    --set crypto.vault.address=https://vault.example.com:8200 \
    --set crypto.vault.transitKey=aifactory \
    --set oidc.issuer=https://keycloak.example.com/realms/example \
    --set oidc.clientId=aifactory \
    --set ingress.host=aifactory.example.com
```

### Verification gate (vanilla + Vault)

```bash
kubectl -n aifactory get pods
kubectl -n aifactory logs deploy/aifactory-web | grep -iE 'vault|kms'
kubectl -n aifactory port-forward svc/aifactory-web 8080:80 &
curl -s http://localhost:8080/api/health | jq .
```

---

## Optional features

After the base install is verified, enable opt-in features by upgrading the release with additional Helm values.

### Tenant Isolation Mode (Epic #35 #36)

```bash
helm upgrade aifactory ./charts/aifactory -n aifactory \
    --reuse-values \
    --set tenant.isolationEnabled=true \
    --set tenant.gatekeeperEnabled=true \
    --set tenant.deletionGraceDays=30 \
    --set tenant.vault.approleRoleIdSecret=aifactory-vault-approle/role-id \
    --set tenant.vault.approleSecretIdSecret=aifactory-vault-approle/secret-id
```

Reference: [`../../docs/docs/concepts/tenant-isolation.md`](../../docs/docs/concepts/tenant-isolation.md).

### LiteLLM gateway (Epic #35 #38)

```bash
helm upgrade aifactory ./charts/aifactory -n aifactory \
    --reuse-values \
    --set litellm.enabled=true \
    --set litellm.config.providers[0].name=openai \
    --set litellm.config.providers[0].apiKeySecret=openai-key
```

Reference: [`../../docs/docs/concepts/litellm-gateway.md`](../../docs/docs/concepts/litellm-gateway.md). Dashboard JSON: `charts/aifactory/dashboards/litellm.json`.

### SAML 2.0 + SCIM 2.0 (Epic #35 #41)

```bash
helm upgrade aifactory ./charts/aifactory -n aifactory \
    --reuse-values \
    --set saml.enabled=true \
    --set saml.idp.metadataUrl=https://idp.example.com/metadata \
    --set scim.enabled=true \
    --set scim.bearerTokenSecret=scim-bearer
```

Reference: [`../../docs/docs/concepts/saml-scim.md`](../../docs/docs/concepts/saml-scim.md). For v1.2 SAML SLO: add `--set saml.slo.enabled=true`.

### Multi-replica fan-out (Epic #35 #40)

```bash
helm upgrade aifactory ./charts/aifactory -n aifactory \
    --reuse-values \
    --set redis.enabled=true \
    --set redis.url=redis://redis.aifactory.svc.cluster.local:6379/0 \
    --set replicas=3
```

Reference: [`../../docs/docs/concepts/multi-replica.md`](../../docs/docs/concepts/multi-replica.md).

### Audit-chain anchor (Epic #35 #43)

```bash
helm upgrade aifactory ./charts/aifactory -n aifactory \
    --reuse-values \
    --set audit.anchor.enabled=true \
    --set audit.anchor.kmsKeyId=<kms-arn-or-vault-key>
```

Reference: [`../../docs/docs/concepts/audit-anchor.md`](../../docs/docs/concepts/audit-anchor.md).

### OTel tracing (Epic #35 #42)

```bash
helm upgrade aifactory ./charts/aifactory -n aifactory \
    --reuse-values \
    --set otel.enabled=true \
    --set otel.endpoint=http://otel-collector.observability.svc.cluster.local:4317
```

Reference: [`../../docs/docs/concepts/observability-tracing.md`](../../docs/docs/concepts/observability-tracing.md).

---

## End-to-end smoke test

After the base install + any opt-in features, run this short test to prove the deployment can carry a real workload:

1. **Browse** to `https://aifactory.example.com`; you should be redirected to your IdP.
2. **Log in**; first user becomes the org owner automatically.
3. **Create a small task** — e.g. "echo 'hello' into a file called test.txt".
4. **Watch the task complete** in the UI; the agent pod should spin up, run, and terminate.
5. **Query the audit log** via `GET /api/audit/export?limit=10` to confirm the action was logged.
6. **Run the audit-chain verifier** if you enabled the anchor:
   ```bash
   kubectl -n aifactory exec deploy/aifactory-web -- python -m server.audit verify-chain
   ```
   Expected: `verified=True`.

If all six steps pass, the install is operationally ready.

---

## Rollback procedure

If the install fails midway or post-install verification fails:

```bash
helm rollback aifactory 0 -n aifactory
kubectl -n aifactory get pods -w
```

For destructive cleanup (only if you want to start over):

```bash
helm uninstall aifactory -n aifactory
kubectl delete namespace aifactory
# Postgres data persists — drop the database if you want a true clean slate.
```

For upgrade rollback rather than fresh-install rollback, see [`upgrade.md`](./upgrade.md).

---

## Troubleshooting

| Symptom                                                                                | Likely cause                                                | Fix                                                                                                                            |
| -------------------------------------------------------------------------------------- | ----------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| `helm install` fails with "no CNI capability detected".                                | Pre-install hook detected neither Calico nor Cilium.        | Install Calico or Cilium (or remove `tenant.isolationEnabled=true` from your values).                                          |
| Web pod CrashLoopBackOff with `crypto backend handshake failed`.                       | KMS IAM / Workload Identity not bound correctly.            | Verify the ServiceAccount annotations + the IAM policy attached to the role.                                                    |
| OIDC redirect fails with `redirect_uri mismatch`.                                      | IdP-configured redirect URI does not match your ingress host. | Add `https://aifactory.example.com/api/auth/oidc/callback` to the IdP's allowed redirect URIs.                                |
| First-login user is not the org owner.                                                 | Default org seeding skipped.                                | Set `bootstrap.firstUserAsOwner=true` (the chart default) and re-run `helm upgrade`.                                            |
| `kubectl exec` into agent pod fails with permission denied.                            | Per-tenant ServiceAccount lacks `exec` permission.          | Use the web pod's debug-shell endpoint instead; tenant SAs are intentionally restricted.                                       |
| Postgres connection from web pod fails with TLS handshake error.                       | RDS / Cloud SQL forces SSL but the URL doesn't request it.  | Append `?sslmode=require` to `DATABASE_URL`.                                                                                  |
| Vault reconciler fails with "permission denied" on `sys/policies/acl/aifactory-tenant-*`. | AppRole policy missing MANAGE capabilities.                 | Re-apply the policy from the pre-flight section.                                                                              |
| `helm upgrade` hangs at "waiting for resource to be ready".                            | Pre-install hook running CNI probe is taking time.          | Wait up to 60 s. If still hung, check `kubectl -n aifactory get jobs` for the pre-install hook job's status.                  |
| Browser cannot reach UI despite Ingress + Service being correct.                       | NetworkPolicy denying ingress controller pod-to-pod.        | Add an explicit `from: namespaceSelector: kubernetes.io/metadata.name: ingress-nginx` rule to the chart's ingress NetworkPolicy. |

---

## Related documentation

- [`upgrade.md`](./upgrade.md) — upgrade between releases.
- [`../operations/image-mirroring.md`](../operations/image-mirroring.md) — mirror images for air-gapped clusters.
- [`../operations/audit-trail.md`](../operations/audit-trail.md) — audit chain + anchor operator guide.
- [`../operations/kms-rotation-runbook.md`](../operations/kms-rotation-runbook.md) — KMS root-key rotation.
- [`../security/threat-model.md`](../security/threat-model.md) — STRIDE per-component.
- [`../compliance/soc2-evidence.md`](../compliance/soc2-evidence.md) / [`../compliance/iso27001-evidence.md`](../compliance/iso27001-evidence.md) — audit evidence maps.
- All Docusaurus concept docs: [`../../docs/docs/concepts/`](../../docs/docs/concepts/) — per-feature deep dives.
