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
| `AIFACTORY_AUTO_PR` | off | Auto-open a pull request on a clean build. |
| `AIFACTORY_AUTO_MERGE` | off | Auto-merge after the reviewer approves. Requires `AIFACTORY_AUTO_PR`. |
| `AIFACTORY_PR_REVIEWER` | `aifactory` | Which review gates the merge: `aifactory` (built-in engine, no Copilot credits), `copilot` (GitHub Copilot review), or `any` (any approved GitHub review). See [`guides/pr-endgame.md`](https://github.com/olafkfreund/AIFactory/blob/dev/guides/pr-endgame.md). |

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
| `AIFACTORY_PARALLEL` / `--parallel` / `workers` | off / 3 | Run independent subtasks as isolated sub-worktrees, merged sequentially. See [Task lifecycle](./task-lifecycle). |
| `AIFACTORY_SOLO_MODE` | off | Single self-directed agent for small jobs (token-saving). |
| `BMAD_SESSION_SEGMENTATION` | off | Per-story session segmentation for large tasks. |

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
