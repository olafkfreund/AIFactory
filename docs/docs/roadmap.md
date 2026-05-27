---
title: Roadmap
sidebar_position: 5
---

# Roadmap

What we're working on, in priority order.

## Recently shipped

- **Epic #92 — Delegation (Copilot + Duo)** ✅ — hand the coder phase off to GitHub Copilot or GitLab Duo Workflow while AIFactory keeps the planning + governance. See the [Delegation concept page](./concepts/delegation).
- **Epic #82 — Portal-managed Git clones** ✅ — clone repos into the portal's workspace root (env-aware on laptop vs Helm PVC), with encrypted-at-rest stored credentials for private repos. See [Portal-managed clones](./concepts/portal-clones).
- **Issue #154 — Scoped MCP API keys** ✅ — replace the host-wide admin token with per-developer scope-gated `acw_` keys for the stdio MCP. See [MCP API keys](./concepts/mcp-stdio-keys).

## Shipping now (stacked PRs)

- **#67 — rmux R0b**: async wrapper around rmux CLI
- **#68 — rmux R1**: per-task session lifecycle + WebSocket bridge
- **#69 — rmux R2**: frontend Live Console tab + Attach UX
- **#70 — rmux R3**: bundled rmux binary + Helm toggle + dual-image CI
- **#71 — rmux R4**: Playwright E2E for the Live Console

When this stack lands on `dev`, the [Live Agent Console](./concepts/rmux-live-console) becomes a first-class feature.

## Next quarter

- **Epic #50 — MCP Control-Plane Tools**: expose AIFactory itself as an MCP server so Claude Code can create projects, kick off builds, and read QA reports without leaving the terminal.
- **Epic #35 — Enterprise v1.1**: tenant isolation, multi-org picker, audit log enrichment.
- **First-class Linear integration**: bidirectional sync (today is one-way GitHub import only).
- **Algolia DocSearch** on this docs site.
- **Browser-side runtime config** so the frontend can talk to a portal at a custom origin without a rebuild.

## On the radar

- **Per-org rate limiting** for shared deployments
- **Cost dashboard** — aggregate token spend by phase, model, agent profile
- **Plan templates** — reusable subtask scaffolds for recurring chores (CRUD endpoints, schema migrations, etc.)
- **Vendor lockfile review** — automated check that your provider mix in `phaseModels` doesn't lock you to a single vendor

## Deferred

- **Multi-tenancy** beyond per-org scoping (cross-org sharing, etc.) — only if there's pull from real users
- **In-browser code editor as primary surface** — VS Code already does this well; we focus on agent orchestration
- **Hosted SaaS offering** — depends on demand; self-hosted Helm is the supported path

## How decisions get made

Roadmap changes go through:

1. Open a GitHub Issue with the proposal
2. Discuss in the issue or in the project's GitHub Discussions
3. A maintainer either lands it on the roadmap or closes with explanation

We don't add features that aren't on this roadmap unless they fix a bug or unblock a real user. If you have a use case the roadmap doesn't cover, **open an issue first**.
