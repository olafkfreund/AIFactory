# Deployment Runbook

> Audience: SRE / platform teams installing AIFactory v1.0.
> Time budget: 60-90 minutes per environment (excluding cloud
> provisioning time).
> Status: v1.0 (closing Epic #26).

This runbook covers the four supported production paths. Each has
identical AIFactory configuration; the differences are in **what
managed services** the operator wires up.

| Path | Postgres | KMS | OIDC IdP | Validated by |
| --- | --- | --- | --- | --- |
| **EKS + RDS** | Amazon RDS for PostgreSQL | AWS KMS | Okta / Azure AD / Keycloak | This runbook §1 |
| **AKS + Azure Postgres** | Azure Database for PostgreSQL | Azure Key Vault | Microsoft Entra ID | This runbook §2 |
| **GKE + Cloud SQL** | Google Cloud SQL for PostgreSQL | GCP Cloud KMS | Google Workspace / Okta | This runbook §3 |
| **Vanilla K8s + self-managed** | CloudNativePG / external | HashiCorp Vault Transit | Keycloak | This runbook §4 |

## Pre-flight checklist (all paths)

- [ ] Kubernetes 1.27+
- [ ] Helm 3.16+
- [ ] cosign 2.2+ (signature verification at install time)
- [ ] External Secrets Operator installed cluster-wide
- [ ] Ingress controller (nginx / ALB / Application Gateway / GCLB)
- [ ] Pod Security admission with `restricted` enforcement on the
      target namespace
- [ ] Network egress to:
        - `*.anthropic.com:443` (LLM provider)
        - your IdP discovery URL
        - your KMS endpoint
- [ ] Pre-pulled AIFactory image OR mirror-registry access (see
      `guides/operations/image-mirroring.md` for `cosign copy`)

---

## §1. EKS + RDS + AWS KMS

### Provision the cloud resources

```bash
# 1. RDS PostgreSQL instance (production-grade)
aws rds create-db-instance \
    --db-instance-identifier aifactory-prod \
    --db-instance-class db.t3.medium \
    --engine postgres --engine-version 16.4 \
    --master-username aifactory_admin \
    --master-user-password "$(openssl rand -base64 32)" \
    --allocated-storage 100 \
    --storage-encrypted \
    --backup-retention-period 14 \
    --multi-az \
    --vpc-security-group-ids sg-xxxx \
    --db-subnet-group-name aifactory-private

# 2. KMS CMK
aws kms create-key \
    --description "aifactory-prod root" \
    --key-policy file://kms-policy.json
aws kms create-alias \
    --alias-name alias/aifactory-prod \
    --target-key-id <key-id>

# 3. IAM role for the app pod (IRSA)
aws iam create-role --role-name aifactory-prod \
    --assume-role-policy-document file://trust-policy.json
aws iam put-role-policy --role-name aifactory-prod \
    --policy-name kms-encrypt-decrypt \
    --policy-document '{"Version":"2012-10-17","Statement":[{
      "Effect":"Allow",
      "Action":["kms:Encrypt","kms:Decrypt"],
      "Resource":"<cmk-arn>"
    }]}'
```

### Pre-seed Secrets via ExternalSecrets

```yaml
apiVersion: external-secrets.io/v1beta1
kind: ClusterSecretStore
metadata:
  name: aifactory-aws-sm
spec:
  provider:
    aws:
      service: SecretsManager
      region: eu-west-1
      auth:
        jwt:
          serviceAccountRef:
            name: external-secrets-sa
            namespace: external-secrets
---
# Seed in AWS Secrets Manager out-of-band:
#   aifactory/db.url        — postgresql+asyncpg://...
#   aifactory/oidc.secret   — <from Okta admin console>
```

### values-eks.yaml

```yaml
image:
  repository: <your-mirror>/aifactory
  tag: "1.0.0"

postgres:
  bundled: false
  externalSecretName: aifactory-db

externalSecrets:
  enabled: true
  backend: aws-sm
  clusterSecretStoreName: aifactory-aws-sm
  refs:
    databaseUrl: "aifactory/db:url"
    oidcClientSecret: "aifactory/oidc:secret"

oidc:
  enabled: true
  provider: okta
  issuerUrl: "https://YOUR_TENANT.okta.com/oauth2/default"
  clientId: "<okta-client-id>"

kms:
  backend: aws_kms
  awsKmsKeyId: "arn:aws:kms:eu-west-1:1234:key/abcd-1234"

serviceAccount:
  annotations:
    eks.amazonaws.com/role-arn: "arn:aws:iam::1234:role/aifactory-prod"

ingress:
  enabled: true
  className: alb
  annotations:
    alb.ingress.kubernetes.io/scheme: internal
    alb.ingress.kubernetes.io/ssl-redirect: "443"
  hosts:
    - host: aifactory.internal.bank.com
      paths: [{path: /, pathType: Prefix}]
```

### Install

```bash
helm install aifactory ./charts/aifactory -f values-eks.yaml \
    --wait --timeout=10m

# Smoke test
kubectl exec deploy/aifactory -- curl -sf http://localhost:3101/api/health
```

---

## §2. AKS + Azure Postgres + Azure Key Vault

### Provision the cloud resources

```bash
# Azure Database for PostgreSQL Flexible Server
az postgres flexible-server create \
    --resource-group aifactory-prod \
    --name aifactory-prod \
    --version 16 \
    --tier GeneralPurpose --sku-name Standard_D2ds_v5 \
    --storage-size 128 \
    --backup-retention 14 \
    --high-availability ZoneRedundant

# Key Vault + key
az keyvault create --name kv-aifactory-prod --resource-group aifactory-prod
az keyvault key create --vault-name kv-aifactory-prod \
    --name aifactory-root --kty RSA --size 2048 \
    --ops wrapKey unwrapKey

# Workload identity for the pod
az identity create --name aifactory-prod -g aifactory-prod
# Grant wrapKey + unwrapKey on the key:
az role assignment create \
    --assignee <managed-identity-principal-id> \
    --role "Key Vault Crypto User" \
    --scope "/subscriptions/.../keys/aifactory-root"
```

### values-aks.yaml

```yaml
image:
  repository: <your-mirror>/aifactory
  tag: "1.0.0"

externalSecrets:
  enabled: true
  backend: azure-kv
  clusterSecretStoreName: aifactory-azure-kv

oidc:
  enabled: true
  provider: azure_ad
  issuerUrl: "https://login.microsoftonline.com/<tenant>/v2.0"
  clientId: "<app-id>"
  # See guides/operations/oidc-setup.md § Azure AD for group claim setup.

kms:
  backend: azure_kv
  azureKeyvaultUrl: "https://kv-aifactory-prod.vault.azure.net"
  azureKeyvaultKey: "aifactory-root"

serviceAccount:
  annotations:
    azure.workload.identity/client-id: "<managed-identity-client-id>"
```

Rest of the install mirrors §1's `helm install`.

---

## §3. GKE + Cloud SQL + Cloud KMS

### Provision

```bash
# Cloud SQL Postgres
gcloud sql instances create aifactory-prod \
    --database-version=POSTGRES_16 \
    --tier=db-custom-2-7680 \
    --region=europe-west1 \
    --backup-start-time=02:00 \
    --availability-type=REGIONAL

# Cloud KMS
gcloud kms keyrings create aifactory --location global
gcloud kms keys create aifactory-root \
    --keyring aifactory --location global \
    --purpose encryption

# Workload identity for pod
gcloud iam service-accounts create aifactory-prod
gcloud kms keys add-iam-policy-binding aifactory-root \
    --location global --keyring aifactory \
    --member "serviceAccount:aifactory-prod@my-project.iam.gserviceaccount.com" \
    --role roles/cloudkms.cryptoKeyEncrypterDecrypter
```

### values-gke.yaml

```yaml
kms:
  backend: gcp_kms
  gcpKmsKeyName: "projects/my-project/locations/global/keyRings/aifactory/cryptoKeys/aifactory-root"

serviceAccount:
  annotations:
    iam.gke.io/gcp-service-account: "aifactory-prod@my-project.iam.gserviceaccount.com"

externalSecrets:
  backend: gcp-sm

oidc:
  enabled: true
  provider: keycloak  # Workspace OIDC via Keycloak federation
  # ... or use Google directly via custom OIDC config
```

---

## §4. Vanilla K8s + self-managed Postgres + Vault

### Provision

```bash
# CloudNativePG cluster (operator-deployed cluster)
kubectl apply -f - <<EOF
apiVersion: postgresql.cnpg.io/v1
kind: Cluster
metadata:
  name: aifactory-prod
spec:
  instances: 3
  storage:
    size: 100Gi
    storageClass: ssd
  backup:
    barmanObjectStore:
      destinationPath: s3://aifactory-backups/postgres
  postgresql:
    parameters:
      max_connections: "100"
EOF

# Vault Transit
vault secrets enable -path=transit transit
vault write -f transit/keys/aifactory-root type=aes256-gcm96
vault policy write aifactory-app - <<EOF
path "transit/encrypt/aifactory-root" { capabilities = ["update"] }
path "transit/decrypt/aifactory-root" { capabilities = ["update"] }
EOF

# Kubernetes auth method for the app SA
vault write auth/kubernetes/role/aifactory-prod \
    bound_service_account_names=aifactory-prod \
    bound_service_account_namespaces=aifactory \
    policies=aifactory-app \
    ttl=1h
```

### values-vanilla.yaml

```yaml
externalSecrets:
  enabled: true
  backend: vault
  clusterSecretStoreName: aifactory-vault

oidc:
  enabled: true
  provider: keycloak
  issuerUrl: "https://keycloak.internal/realms/aifactory"
  clientId: "aifactory-web"

kms:
  backend: vault_transit
  vaultTransitKey: "aifactory-root"

postgres:
  externalSecretName: aifactory-db  # CNPG creates this Secret automatically
```

---

## Day-2 operations

| Operation | Procedure |
| --- | --- |
| **Backup** | Cloud-provider automatic + verify monthly via `scripts/drills/backup-restore.sh` |
| **Upgrade** | See `guides/deployment/upgrade.md`. Run `scripts/drills/upgrade-in-place.sh --dry-run` first. |
| **KMS rotation** | `guides/operations/kms-rotation-runbook.md` — annual or on compromise |
| **Audit log retention** | Daily job; verify via `python -m server.audit verify-chain` |
| **Monitor** | Import `guides/observability/grafana-aifactory.json` to Grafana |

## Verification gate

A successful install must pass ALL of:

```bash
# 1. Pod healthy
kubectl get deploy/aifactory -o jsonpath='{.status.readyReplicas}'  # → 1

# 2. /api/health returns 200
kubectl exec deploy/aifactory -- curl -sf http://localhost:3101/api/health

# 3. /metrics scrape works
kubectl exec deploy/aifactory -- curl -sf http://localhost:3101/metrics | head -5

# 4. OIDC discovery reachable
kubectl exec deploy/aifactory -- curl -sf \
    https://your-idp/realms/aifactory/.well-known/openid-configuration | jq .issuer

# 5. KMS reachable (via app)
kubectl logs deploy/aifactory | grep -i "kms" | head -3
# Expected: no errors. Example: "init: kms backend = aws_kms"

# 6. Audit chain verifies
curl -fsSL -H "Authorization: Bearer $TOKEN" \
    https://aifactory/api/audit/export?format=json | \
    python -m server.audit verify-chain /dev/stdin
# Expected: "OK: N rows verified"
```

## Reviewer signoff

| Role | Name | Date | Notes |
| --- | --- | --- | --- |
| Author | Olaf Krasicki-Freund | 2026-05-25 | v1.0 runbook for 4 cloud paths |
| Walkthrough (non-author) | _TBD via PR review_ | _TBD_ | Required for v1.0 acceptance |
