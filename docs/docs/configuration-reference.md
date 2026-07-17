---
title: Configuration Reference
sidebar_position: 4
---

# Configuration Reference

A single place to find the environment flags and settings that change
AIFactory's behaviour. Most feature flags are **opt-in and default off** — the
core pipeline works with none of them set.

Flags are read in two places:

- **Web server environment** (`apps/web-server/.env` or pod env) — host-wide.
- **Per-project settings** (`.aifactory/.env` in the target repo) — override the
  host for one project. The PR-endgame and deploy flags are read here.

## PARR endgame — deploy, PR, merge

| Flag | Default | What it does |
|------|---------|--------------|
| `AIFACTORY_AUTO_DEPLOY` | off | After a clean build, deploy to AWS App Runner with deterministic Terraform, verify the live endpoint, then tear down. See [Deploy-then-verify](./concepts/deploy-then-verify). |
| `AIFACTORY_AUTO_PR` | off | Auto-open a pull request on a clean build. For issue-intake builds the PR targets the repo's `base_branch` from [`AIFACTORY_INTAKE_REPOS`](#issue-intake) (default `main`) and the PR body carries `Fixes #N` for the origin issue. |
| `AIFACTORY_AUTO_MERGE` | off | Auto-merge after the reviewer approves. Requires `AIFACTORY_AUTO_PR`. |
| `AIFACTORY_PR_REVIEWER` | `aifactory` | Which review gates the merge: `aifactory` (built-in engine, no Copilot credits), `copilot` (GitHub Copilot review), or `any` (any approved GitHub review). See [`guides/pr-endgame.md`](https://github.com/olafkfreund/AIFactory/blob/dev/guides/pr-endgame.md). |

## Issue intake

The RFC-0011 intake poller watches configured repos for `factory:*`-labelled
issues and turns them into builds via `/api/tasks/from-issue`. Intake is
idempotent by issue number: redelivery of the same issue returns the existing
task (`deduplicated: true`) instead of creating a duplicate build.

| Flag | Default | What it does |
|------|---------|--------------|
| `AIFACTORY_INTAKE_POLLER` | off | Enable the background intake poller. |
| `AIFACTORY_INTAKE_REPOS` | (none) | JSON list of repos to watch — see below. |
| `AIFACTORY_INTAKE_INTERVAL_S` | 30 | Poll interval in seconds. |
| `AIFACTORY_INTAKE_AUTO_HANDOFF` | on | Hand finished intake builds to TFactory for independent verification (no-op unless `TFACTORY_BASE_URL` is set). |

Each `AIFACTORY_INTAKE_REPOS` entry:

```json
{
  "provider": "github",
  "repo": "owner/name",
  "project_id": "my-project",
  "change_mode": "existing",
  "base_branch": "dev"
}
```

`change_mode` and `base_branch` are optional. `base_branch` names the repo's
integration branch: it is threaded through from-issue into the task metadata
and the auto-PR endgame, so PRs land on the branch the repo integrates on
(default `main`).

### The `factory:*` label taxonomy

Only issues carrying at least one `factory:*` label are picked up. Scoped
spellings (`factory::hard`) are accepted throughout.

| Label | Effect |
|-------|--------|
| `factory:low` / `factory:medium` | Difficulty tier — build directly in AIFactory. |
| `factory:hard` | Route the issue to PFactory for full planning first. |
| `factory:parallel` | Run the build's independent subtasks as a concurrent wave. |
| `factory:serial` | Force serial; wins over every other parallel control. |
| `factory:workers=N` | Cap wave workers at N (tunes only; pair with `factory:parallel`). |

## Auth

| Flag | Default | What it does |
|------|---------|--------------|
| `AIFACTORY_ALLOW_API_KEY` | off | Opt into direct `ANTHROPIC_API_KEY` auth. Default is OAuth-only and the key is scrubbed from agents. See [API-key auth](./concepts/api-key-auth). |

## MCP control plane

| Flag | Default | What it does |
|------|---------|--------------|
| `AIFACTORY_MCP_REMOTE_ENABLED` | off | Expose the HTTP+SSE MCP server at `/api/mcp-remote/sse` for non-Claude clients. |

Scoped MCP keys (`acw_`) gate remote tools with `mcp:read` / `mcp:write`. See
[Scoped MCP keys](./concepts/mcp-stdio-keys).

## Live Console (rmux)

| Flag | Default | What it does |
|------|---------|--------------|
| `AIFACTORY_RMUX_ENABLED` / `APP_RMUX_ENABLED` | off | Enable the streaming rmux Live Agent Console. See [Live Console](./concepts/rmux-live-console). |
| `AIFACTORY_RMUX_PANES_DIR` | (auto) | Writable directory for the rmux FIFO panes (defaults to the data dir). |

## Security & sandboxing

| Flag | Default | What it does |
|------|---------|--------------|
| `AIFACTORY_BASH_SANDBOX` | on | Gate the bubblewrap syscall sandbox. Set off on k3d/Kind nodes where bwrap can't mount `/proc`. |
| `AIFACTORY_EXTRA_ALLOWED_COMMANDS` | (none) | Operator override to extend the dynamic command allowlist. |

## Execution mode

| Flag | Default | What it does |
|------|---------|--------------|
| `run.py --parallel` / `--workers N` | off / 3 | Run a wave's independent subtasks as isolated sub-worktrees, merged sequentially. The scheduler only puts subtasks touching disjoint files in the same wave. See [Task lifecycle](./task-lifecycle). |
| `AIFACTORY_INTAKE_PARALLEL` | off | Fleet default for **issue-intake** builds (`/api/tasks/from-issue`): when truthy (`1`/`true`/`yes`/`on`), an unlabelled issue builds in parallel. Per-issue labels always win — see [Parallel execution for intake builds](#parallel-execution-for-intake-builds). |
| `AIFACTORY_INTAKE_WORKERS` | (unset) | Worker cap for intake builds that run parallel. Unset leaves the coder's own default (3). Ignored by serial builds. |
| `AIFACTORY_SOLO_MODE` | off | Single self-directed agent for small jobs (token-saving). |
| `BMAD_SESSION_SEGMENTATION` | off | Per-story session segmentation for large tasks. |

### Parallel execution for intake builds

Parallelism is opted into **per issue**, on a separate axis from the RFC-0011
difficulty tier (`factory:low` / `factory:medium` / `factory:hard`) — any tier
can run parallel or serial.

| Label | Effect |
|-------|--------|
| `factory:parallel` | Run this build's independent subtasks concurrently. |
| `factory:serial` | Force serial. Always wins over `factory:parallel` and over `AIFACTORY_INTAKE_PARALLEL`. |
| `factory:workers=N` | Cap the workers at N. Tunes only — it does **not** enable parallelism on its own; pair it with `factory:parallel`. |

Scoped label spellings (`factory::parallel`) are accepted, matching the tier
labels. A per-project default can also be set from the portal
(**Settings → General**, "Parallel build execution" toggle + worker count,
workers 1–8). Resolution order, most specific first:

`factory:serial` > `factory:parallel` > portal setting > `AIFACTORY_INTAKE_PARALLEL` > off.

The portal toggle outranks the env default on purpose — it is the user's own
choice and must visibly win over a fleet-wide default. The resolved setting is
written to the spec's `task_metadata.json` (`parallel` / `workers`), which the
agent service reads back when it starts any build — so the same setting also
applies on the auto-continue, plan-approval and queue-drain paths.

The wave path has run on live intake builds: three concurrent workers with all
merge-backs kept, 21.4 minutes against a ~35-minute serial baseline for the
same task (3.6.50–3.6.52). Still opt-in per issue or per project; flip
`AIFACTORY_INTAKE_PARALLEL` to make it the fleet default.

## Job-native build & multi-node scheduling

How the coder loop (`run.py`) is executed, and what it takes for a build to schedule on any node rather than being pinned to one. See [Multi-replica deployment](./concepts/multi-replica) and [Reproducible builds in a per-task Nix env](./nix-reproducible-build).

| Flag | Default | What it does |
|------|---------|--------------|
| `AIFACTORY_BUILD_BACKEND` | `subprocess` | How `run.py` runs. `subprocess` is the in-pod asyncio subprocess (the shipped code default, kept as the safe fallback). `kubejob` dispatches each build as its own Kubernetes Job with Job-native log streaming. The reference live deployment sets `kubejob`. |
| `AIFACTORY_PACK_WORKSPACE` | off | On the `kubejob` path, pack the populated `/work` to object storage and have the Job unpack it into a writable `emptyDir`, instead of co-mounting the workspace RWO `local-path` PVC. Removes the workspace node-pin — the first half of multi-node scheduling. |
| `AIFACTORY_PACKED_NIX_IN_IMAGE` | off | On the packed path, drop the warm Nix-store RWO `local-path` PVC from the build Job and resolve `/nix` from the build image instead. Removes the last node-pin; a packed build Job then carries no node affinity. Pair with `AIFACTORY_BUILD_IMAGE` pointed at a `-nix` tag. |
| `AIFACTORY_BUILD_IMAGE` | (unset) | Overrides the image used for the `kubejob` build Job only (precedence: `AIFACTORY_BUILD_IMAGE` > `AIFACTORY_IMAGE` > built-in default). Point it at a published `:sha-<short>-nix` tag — the runtime image plus a baked `/nix/store` — so the build sources Nix from the layer rather than the PVC. |

## Tenancy

| Flag | Default | What it does |
|------|---------|--------------|
| `AIFACTORY_MULTI_TENANT` | off | Application-level tenant scoping. The tenant is resolved from the `X-Tenant-Id` request header (default `"default"`), stamped into task metadata at intake and `/execution`, filtered on task/project list endpoints, and carried on the TFactory handoff payload. Flag off is unchanged behaviour. |

This is per-request scoping inside one deployment. For infrastructure-level
separation (namespaces, S3 prefixes, Vault paths per organization) see
[Tenant Isolation Mode](./concepts/tenant-isolation).

## Trusted-plan ingest

| Flag | What it does |
|------|--------------|
| `AIFACTORY_TRUSTED_PLAN_KEY_<AUTHORITY>` | HMAC key used to verify a signed Task Contract v2 from an upstream authority (e.g. `PFACTORY`). See [Task Contract](./task-contract). |

## Per-project settings (portal UI)

These map to keys in the project's `.aifactory/.env` and can be set from
**Settings → General** in the portal:

| Setting | Key | Default |
|---------|-----|---------|
| Auto-open a PR | `AIFACTORY_AUTO_PR` | off |
| Auto-merge after approval | `AIFACTORY_AUTO_MERGE` | off |
| Pre-merge reviewer | `AIFACTORY_PR_REVIEWER` | `aifactory` |
| Auto-deploy on build | `AIFACTORY_AUTO_DEPLOY` | off |
| Delegate the coder phase | `DELEGATE_BY_DEFAULT` | off |

:::note
Flag names and defaults are verified against the code at time of writing. The
authoritative source is always the [CHANGELOG](https://github.com/olafkfreund/AIFactory/blob/dev/CHANGELOG.md)
and the modules under `apps/backend/core/` and `apps/web-server/server/services/`.
:::
