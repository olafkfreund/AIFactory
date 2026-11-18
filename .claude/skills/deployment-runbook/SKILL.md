---
name: deployment-runbook
description: >
  Guide an operator through AIFactory's Helm deployment runbook for a
  Kubernetes cluster.
when_to_use: >
  Activate when the user asks about installing AIFactory on Kubernetes,
  choosing its KMS backend, verifying an install, or troubleshooting a
  deployment covered by the runbook.
---

# Deployment runbook

Use [guides/deployment/runbook.md](../../../guides/deployment/runbook.md) as the source of truth. Follow its order:

1. **Pre-flight checklist:** Check the documented Kubernetes, CNI, Helm, Postgres, KMS, OIDC, and registry prerequisites. Tenant isolation adds the documented FQDN policy and Vault reconciler AppRole prerequisites.
2. **KMS backend choice:** Select the runbook's Fernet, AWS KMS, Vault Transit, Azure Key Vault, or GCP KMS option for the environment. Fernet is for dev or sandbox only.
3. **Install:** Use the matching EKS, AKS, GKE, or vanilla Kubernetes and Vault section, including its cluster prerequisites and install steps.
4. **Verification gate:** Complete that section's pod and health checks, plus its KMS or Vault check where specified. Follow the runbook's end-to-end smoke test for SSO, a task, and the audit log.
5. **Troubleshooting:** Use the runbook's symptom table for CNI, KMS, OIDC, ownership, permissions, Postgres TLS, Vault, Helm readiness, and ingress issues.

An agent never runs `helm install` or touches a live cluster without the operator. An agent never handles Vault or KMS secret values.
