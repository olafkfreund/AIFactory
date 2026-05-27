---
title: Portal-managed Git clones
sidebar_position: 8
---

# Portal-managed Git clones

> *Let the portal clone the repo. Required for SaaS / Kubernetes deployments where the user's source tree isn't on AIFactory's filesystem.*

Before epic [#82](https://github.com/olafkfreund/AIFactory/issues/82), every AIFactory project was a path on the portal's own filesystem. That worked fine on laptops where the user and the portal share a disk. It broke as soon as AIFactory moved into a Helm chart on Kubernetes or behind a SaaS gateway — the source tree the user wanted to work on lived on *their* laptop, not on the cluster node.

Portal-managed clones close that gap. Hand the portal a Git URL; it clones the repo into a configurable workspace root, optionally using a stored Personal Access Token for private repos, and registers the local clone as a normal project.

## When to use it

- **You're running AIFactory on Kubernetes.** The portal pod has no access to your laptop; portal-managed clones are the only way for it to see your code.
- **You're running AIFactory as a multi-user SaaS.** Each user needs their own workspace and credentials, isolated from one another.
- **You have repos in private namespaces** (private GitHub repos, GitLab subgroups, etc.) and want a clean credential flow rather than mounting `~/.ssh` into the portal.

If you're on a single-user laptop pointing AIFactory at folders on your own disk, you can keep using the legacy "Add project by path" flow. Both modes are first-class.

## Quick start

1. **Mint a stored credential (optional, for private repos).** Open **Settings → Git Credentials**, click **Add credential**, paste a Personal Access Token, give it a name (`github-deploy-bot`, etc.). The token is encrypted at rest via `EncryptedString` (Epic #26 P2.3) and never returned again — only the credential ID surfaces.
2. **Add a project from Git URL.** Click **Add Project → Clone from Git URL**, paste the HTTPS URL, optionally pick the stored credential, hit **Add**. The portal clones into `$PROJECT_WORKSPACE_ROOT/<repo-name>/`.
3. **Use as normal.** The project shows up in the project list pointing at the local clone path. Tasks, Auto-Fix, agent runs — all unchanged.

The handover workflow also auto-clones now: when you run `/handover` and the destination cwd doesn't match any registered project but you have a stored Git URL, the cwd gets registered as a new project via the same clone path.

## Workspace root

`PROJECT_WORKSPACE_ROOT` env var controls where cloned repos land:

| Deployment | Default workspace root | Override |
|---|---|---|
| Laptop install | `~/.aifactory/workspaces/` | Set `PROJECT_WORKSPACE_ROOT` in `.env` |
| Helm chart on K8s | `/var/lib/aifactory/workspaces/` (PVC) | `workspaces.persistence.path` in `values.yaml` |
| CI runners | `$RUNNER_TEMP/aifactory-workspaces/` | `PROJECT_WORKSPACE_ROOT` env |

Auto-Fix runs `git pull --ff-only` on every poll cycle (5-minute cadence) so the planner sees the latest commits without manual sync.

## Credential model

Stored credentials live in the `git_credentials` table and are scoped per organization:

```
git_credentials
├── id          UUID
├── org_id      FK org
├── name        "github-deploy-bot"
├── host        github.com   (optional — matched against the clone URL host)
├── username    oauth2       (optional)
├── token       EncryptedString (Fernet-encrypted at rest)
└── created_at, last_used_at
```

When the clone service runs with a stored credential, the token is **injected into the HTTPS URL for the network operation only**, then immediately sanitized via `git remote set-url origin <clean-url>` so the credential never persists in the workspace's `.git/config`. Logs never contain the token.

V1 supports HTTPS PATs only. Deploy Keys and GitHub App credentials are tracked as follow-ups to epic #82.

## Helm deployment

For K8s installs, enable the workspaces PVC in `values.yaml`:

```yaml
workspaces:
  enabled: true
  persistence:
    size: 50Gi
    storageClass: standard
    path: /var/lib/aifactory/workspaces
```

The chart mounts the PVC at the configured path and sets `PROJECT_WORKSPACE_ROOT` on the deployment. Survives pod restarts and rolling updates. See `charts/aifactory/values.yaml` for the full schema.

## Related

- **MCP project_create tool** — the stdio MCP exposes the same clone path via `project_create(gitUrl, gitCredentialId)`, so an agent running via `/handover` can register itself.
- **Scoped MCP keys** — combine portal-managed clones with [scoped MCP API keys](./mcp-stdio-keys.md) for full enterprise isolation: each developer holds a scoped `acw_` key + their stored credentials, the host-wide admin token stays unused.

Related epic + sub-issues: [#82](https://github.com/olafkfreund/AIFactory/issues/82) (epic) · [#152](https://github.com/olafkfreund/AIFactory/pull/152) (PR-A schema + clone service) · [#155](https://github.com/olafkfreund/AIFactory/pull/155) / [#156](https://github.com/olafkfreund/AIFactory/pull/156) (PR-B PVC + frontend) · [#157](https://github.com/olafkfreund/AIFactory/pull/157) (PR-C credentials table) · [#158](https://github.com/olafkfreund/AIFactory/pull/158) (Settings page).
