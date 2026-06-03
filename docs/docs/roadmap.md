---
title: Roadmap
sidebar_position: 5
---

# Roadmap

What we're working on, in priority order.

## Direction

AIFactory is an **open-source, open-core** project. The strategic priority is **adoption** — getting
the self-hostable, review-gated, auditable core into the hands of the engineers who need it most:
platform and security teams at organizations that can't send their code to a cloud agent. See
[Why AIFactory](./why-aifactory) for the positioning, and the
[GTM strategy memo](https://github.com/olafkfreund/AIFactory/blob/dev/docs/plans/2026-05-30-oss-gtm-strategy.md)
for the full reasoning.

What that means for the roadmap, concretely:

- **The core stays free and open.** The pipeline, web UI, multi-provider routing, worktree
  isolation, and single-tenant self-hosting are the project — and they stay MIT.
- **Near-term focus is the adoption path**, not more enterprise depth: a frictionless first-run
  (one-command self-host, local-model support), a verifiable end-to-end demo, and docs that lead
  with the problem. Enterprise features deepen as real demand pulls them.
- **The enterprise edition** (multi-tenant, SAML/SCIM, signed audit anchors + evidence export,
  support) is how the open-source core stays funded — added when organizations ask for it.

## Recently shipped

- **Mission Control workspace + portal UX** ✅ (PR #311) — a full-page three-pane task workspace (plan & subtasks · live activity + embedded console · preview / files / review), opened on demand from the task header. Ships with a portal polish pass: skeleton loaders, a live progress timeline, a theme-aware terminal, a "waiting for you" review beacon, and animated streaming logs. See [Mission Control workspace](./concepts/mission-control-workspace).
- **Epic #35 — Enterprise v1.1** ✅ **9/9 children shipped (2026-05-28)**:
  - #36 Tenant Isolation Mode (per-org K8s namespace + IRSA + gatekeeper samples)
  - #37 gVisor sandbox opt-in
  - #38 LiteLLM gateway + audit hook + PII redactor
  - #39 Bedrock + Vertex provider support
  - #40 S3 workspace storage + Redis pub/sub fan-out
  - #41 SAML 2.0 + SCIM 2.0 (security-foundation + routes + helm blocks)
  - #42 OpenTelemetry distributed tracing
  - #43 Signed audit-chain anchor + ISO 27001 evidence map
  - #154 Scoped MCP API keys
  See the [ISO 27001 evidence map](https://github.com/olafkfreund/AIFactory/blob/dev/guides/compliance/iso27001-evidence.md) + linked concept docs.

- **Epic #92 — Delegation (Copilot + Duo)** ✅ — hand the coder phase off to GitHub Copilot or GitLab Duo Workflow while AIFactory keeps the planning + governance. See the [Delegation concept page](./concepts/delegation).
- **Epic #82 — Portal-managed Git clones** ✅ — clone repos into the portal's workspace root (env-aware on laptop vs Helm PVC), with encrypted-at-rest stored credentials for private repos. See [Portal-managed clones](./concepts/portal-clones).

## Shipping now (stacked PRs)

- **Epic #35 #41 — SAML 2.0 + SCIM 2.0** (60% landed): security-foundation modules
  (replay cache, OneLogin SDK wrapper with XSW + signed-Assertion defences,
  HMAC RelayState, SCIM schemas/filters/auth) merged in PR #177; the
  `external_identities` schema for multi-IdP linkage merged in PR #178.
  Remaining: SAML routes, SCIM CRUD routes, identity-provider dropdown,
  Helm `saml:` + `scim:` blocks. Design doc:
  [`docs/plans/2026-05-28-saml-scim-design.md`](https://github.com/olafkfreund/AIFactory/blob/dev/docs/plans/2026-05-28-saml-scim-design.md).
- **#67 — rmux R0b**: async wrapper around rmux CLI
- **#68 — rmux R1**: per-task session lifecycle + WebSocket bridge
- **#69 — rmux R2**: frontend Live Console tab + Attach UX
- **#70 — rmux R3**: bundled rmux binary + Helm toggle + dual-image CI
- **#71 — rmux R4**: Playwright E2E for the Live Console

When the rmux stack lands on `dev`, the [Live Agent Console](./concepts/rmux-live-console) becomes a first-class feature.

## Next quarter

- **Epic #204 — Enterprise v1.2** (planned improvements to v1.1):
  - Per-tenant LLM budget enforcement + rate-limiting (Claude SDK wrapper)
  - `litellm.audit.scrubBeforeSend` mode (PII removal before LLM vendor)
  - Per-tenant audit-chain anchor + external publication (S3 WORM / RFC 3161)
  - Streaming response audit coverage
  - See tracking issue #204 for full scope.

- **Epic #50 — MCP Control-Plane Tools**: expose AIFactory itself as an MCP server so Claude Code can create projects, kick off builds, and read QA reports without leaving the terminal.
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
