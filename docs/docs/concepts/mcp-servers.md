---
title: Default MCP servers
sidebar_position: 5
---

# Default MCP servers — agents that see your infra

AIFactory's agents work in the codebase by default — they read files, run tests, write commits. But when your project touches Kubernetes, AWS, Azure, or a Git host other than the one the agent's running from, the agent can't *see* the infra. It can't list pods, query an S3 bucket, look up an Azure resource group, or check a GitLab pipeline.

The **default MCP server catalog** fixes that. AIFactory ships a small catalog of well-maintained MCP servers (GitHub, Kubernetes, AWS, Azure as the V1 set; GitLab + Azure DevOps in V1.5) that **auto-enable** when your project's structure indicates the agent needs them AND credentials are detectable.

This means:

- A project with `terraform/` + `aws/` markers gets the AWS MCP server when AWS credentials are present.
- A project with `charts/` or `k8s/` gets the Kubernetes MCP server when a kubeconfig is reachable.
- Every project (since every codebase lives somewhere) gets the GitHub MCP server when a token is set.

No per-task wiring. No per-project setup. If the right markers + credentials are on the box, the right tools are in the agent's hand.

## What ships

| Server | Auto-enables on markers | Credentials it looks for | Default for agents |
|---|---|---|---|
| **github** | always-on | `GITHUB_TOKEN` / `GH_TOKEN` env, or `gh auth login` cache | coder, planner, qa_reviewer, pr_reviewer, spec_gatherer |
| **kubernetes** | `k8s/`, `kubernetes/`, `charts/`, `kustomization.yaml`, `Chart.yaml` | `~/.kube/config`, `KUBECONFIG` env, or in-cluster ServiceAccount | coder, qa_reviewer |
| **aws** | `terraform/`, `*.tf`, `aws/`, `serverless.yml`, `samconfig.toml`, `template.yaml` | `~/.aws/credentials`, `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY` env, or IRSA | coder, qa_reviewer |
| **azure** | `azure/`, `*.bicep`, `azuredeploy.json`, `main.bicep` | `~/.azure/`, `AZURE_TENANT_ID` + `AZURE_CLIENT_ID` + `AZURE_CLIENT_SECRET` env, or Managed Identity | coder, qa_reviewer |
| **gitlab** *(V1.5)* | `.gitlab-ci.yml` | `GITLAB_TOKEN` env (+ optional `GITLAB_INSTANCE_URL` for self-managed) | coder, qa_reviewer |
| **azure_devops** *(V1.5)* | `azure-pipelines.yml`, `.azuredevops/` | `AZURE_DEVOPS_PERSONAL_ACCESS_TOKEN` + `AZURE_DEVOPS_ORG_SERVICE_URL` env | coder, qa_reviewer |

GCP support is a V2 Epic — Google's design is remote-first (managed HTTP endpoints) which is a different transport from V1's stdio subprocess pattern. Tracked in [Epic #100](https://github.com/olafkfreund/AIFactory/issues/100).

## Verify what's enabled

Run the diagnostic CLI from the AIFactory host:

```bash
$ aifactory --mcp-doctor --project-dir /path/to/your/project

MCP catalog status (6 entries)  — project: /path/to/your/project

  ✓ github         creds: env:GITHUB_TOKEN
     markers: always-on
     agents:  coder, planner, qa_reviewer, pr_reviewer, spec_gatherer

  ✓ kubernetes     creds: file:~/.kube/config
     markers: matched: has_kubernetes
     agents:  coder, qa_reviewer

  ✗ aws            creds: file:~/.aws/credentials
     markers: none of: has_aws
     agents:  coder, qa_reviewer

  ✗ azure          creds: none
     markers: none of: has_azure
     agents:  coder, qa_reviewer
     → run 'az login' OR set AZURE_TENANT_ID + AZURE_CLIENT_ID + AZURE_CLIENT_SECRET
```

✓ = both markers and credentials line up (server will auto-enable). ✗ = something's missing — the hint line tells you which.

## How auto-enable works

For each catalog entry, AIFactory checks three things at agent spawn time:

1. **Is the agent eligible?** The catalog says `default_for_agents: ["coder", "qa_reviewer"]` — only those agents get this server. A spec-gatherer agent on the same project still won't see Kubernetes tools.
2. **Does the project have the marker?** A filesystem scan of the project root. The kubernetes entry requires `has_kubernetes`, which matches `k8s/`, `kubernetes/`, `charts/`, etc. (see the table above for the full list).
3. **Are credentials detectable?** A four-layer probe — see "Credential chain" below.

All three must pass. Any one of them fails → the server is silently skipped for that agent on that project.

## Credential chain

AIFactory probes credentials in this order (first hit wins):

1. **Per-project override** — `.aifactory/.env` in the project root. `AGENT_MCP_<agent>_REMOVE=aws` force-disables an otherwise-eligible server; `AGENT_MCP_<agent>_ADD=aws` force-enables it.
2. **Operator config** — `~/.aifactory/mcp-credentials.json`. Points at named env vars or alternate files. Refuses to load with looser-than-0600 perms.
3. **Native CLI discovery** — the files and env vars each cloud's official CLI reads. `~/.aws/credentials`, `~/.kube/config`, `KUBECONFIG`, `AZURE_*` env triplet, `GITHUB_TOKEN`, etc.
4. **K8s in-cluster identity** — service account token at `/var/run/secrets/kubernetes.io/serviceaccount/token`, IRSA (`AWS_ROLE_ARN` + `AWS_WEB_IDENTITY_TOKEN_FILE`), Workload Identity Federation, Managed Identity.

The probes are **cheap** — file existence + env var presence. AIFactory doesn't validate that the credentials WORK (that'd add latency and rate-limit pressure on every spawn). Stale creds surface as MCP-server-side tool errors during the run, visible in the agent's task log.

## Per-project override

Operators can force-enable or force-disable a catalog server with one line in `.aifactory/.env`:

```bash
# Force-enable Kubernetes for this project even if no charts/ dir exists
# (useful when the K8s manifests live in a sibling repo)
AGENT_MCP_coder_ADD=kubernetes
AGENT_MCP_qa_reviewer_ADD=kubernetes

# Force-disable AWS for this project — operator decision (e.g. cost control)
AGENT_MCP_coder_REMOVE=aws

# Disable Context7 for the same project (existing mechanism — catalog
# servers and built-in servers use the same flags)
CONTEXT7_ENABLED=false
```

Per-agent granularity — the same project can give Kubernetes tools to its coder agent but not its qa_reviewer.

## Security recommendations

The credential probes accept whatever the CLI native discovery finds. **What the credentials grant is up to you to scope correctly.** The catalog defaults each server to read-only mode where supported; you supply read-only IAM roles / RBAC where it isn't.

- **GitHub**: use a fine-grained PAT scoped to `read` access only (Issues: read, Pull requests: read, Code: read). The MCP server will surface write attempts as 403s from GitHub.
- **GitLab**: same posture — PAT with `read_api` + `read_repository` scopes.
- **Kubernetes**: provide a kubeconfig that uses a **read-only ServiceAccount** with a `ClusterRole` like `view`. The kubernetes-mcp-server has a `--read-only` flag (AIFactory passes it by default) but defense-in-depth says don't rely on it alone — **CVE-2026-46519 (CVSS 8.8)** in versions before 3.6.0 let the flag be bypassed at the execution layer. AIFactory pins `>=3.6.0` but the RBAC posture is your actual security boundary.
- **AWS**: use an IAM role with `ReadOnlyAccess` (AWS-managed) or a custom policy with only the `Describe*`/`List*`/`Get*` actions you actually need. IRSA is strongly preferred over a long-lived access key.
- **Azure**: assign the **Reader** RBAC role on the relevant resource group / subscription. Service-principal secrets rotate via Microsoft Entra; Managed Identity removes the secret problem entirely.

## Adding custom MCP servers

The catalog is for first-party defaults. If you need a custom server (your internal Splunk MCP, a homegrown SBOM tool, an MCP wrapper around your monitoring stack), AIFactory's existing `CUSTOM_MCP_SERVERS` mechanism handles it. Put the JSON in `.aifactory/.env`:

```bash
CUSTOM_MCP_SERVERS=[
  {"id": "splunk", "type": "command", "command": "npx", "args": ["@yourorg/splunk-mcp"]},
  {"id": "datadog", "type": "http", "url": "https://datadog-mcp.internal/v1/server"}
]
```

Combine with `AGENT_MCP_<agent>_ADD=splunk,datadog` to wire them into specific agents. The custom-server hook predates the catalog and is fully orthogonal — both run side-by-side.

## See also

- [Operator setup for hosted K8s deployments](./mcp-credentials) — Secret schemas, cloud-native identity, rotation procedures, troubleshooting
- [Remote Control](./remote-control) — drive AIFactory agents from claude.ai/code
- [Multi-provider model routing](./multi-provider) — sibling concept for LLM provider selection
