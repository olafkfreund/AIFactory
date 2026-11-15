---
title: Roadmap
sidebar_position: 5
---

# Roadmap

What we're working on, in priority order.

## Direction

AIFactory is an **open-source** project, dual-licensed **MIT OR GPL-3.0**. The strategic priority is **adoption** — getting
the self-hostable, review-gated, auditable core into the hands of the engineers who need it most:
platform and security teams at organizations that can't send their code to a cloud agent. See
[Why AIFactory](./why-aifactory) for the positioning, and the
[GTM strategy memo](https://github.com/olafkfreund/AIFactory/blob/dev/docs/plans/2026-05-30-oss-gtm-strategy.md)
for the full reasoning.

What that means for the roadmap, concretely:

- **The core stays free and open.** The pipeline, web UI, multi-provider routing, worktree
  isolation, and single-tenant self-hosting are the project.
- **Near-term focus is the adoption path**, not more enterprise depth: a frictionless first-run
  (one-command self-host, local-model support), a verifiable end-to-end demo, and docs that lead
  with the problem. Enterprise features deepen as real demand pulls them.
- **Enterprise features** (multi-tenant, SAML/SCIM, signed audit anchors + evidence export)
  are in the same repository and are added when organizations ask for them.

## Recently shipped, August to November 2026

Releases [v3.6.74](https://github.com/olafkfreund/AIFactory/releases/tag/v3.6.74) to
[v3.10.16](https://github.com/olafkfreund/AIFactory/releases/tag/v3.10.16) (2026-10-31). The
summary is [What shipped, August to November](/blog/what-shipped-august-to-november).

- **Auto-merge gated on evidence** — a TFactory verdict plus green CI on the exact head
  commit; high-risk and production-target tasks hold for a human
  ([v3.10.0](https://github.com/olafkfreund/AIFactory/releases/tag/v3.10.0)).
- **The coding agent treated as untrusted** — scrubbed environments, private HOME, pinned
  git config, a fail-closed Bash parser
  ([v3.10.0](https://github.com/olafkfreund/AIFactory/releases/tag/v3.10.0),
  [v3.10.3](https://github.com/olafkfreund/AIFactory/releases/tag/v3.10.3)).
- **Working scoped API keys and enforced OIDC claims**
  ([v3.10.12](https://github.com/olafkfreund/AIFactory/releases/tag/v3.10.12),
  [v3.10.14](https://github.com/olafkfreund/AIFactory/releases/tag/v3.10.14)).
- **Outbound PII scrubbing on by default**
  ([v3.6.75](https://github.com/olafkfreund/AIFactory/releases/tag/v3.6.75)).

### Closed issues from earlier plans

Epic #41 (SAML 2.0 and SCIM 2.0), the rmux Live Console issues #50 and #67 to #71,
and the enterprise follow-up epic #204 are closed. The rmux Live Console is a
shipped feature (see [Live Console](./concepts/rmux-live-console)).

### Earlier: June 2026 — deploy-then-verify + the PR endgame

- **Deploy-then-verify on real AWS** — after the build, ship the services to **AWS App Runner** with deterministic Terraform, verify the live HTTPS endpoint, run the acceptance tests against it, and tear down — cost-guarded with `factory-ephemeral` tags, opt-in via `AIFACTORY_AUTO_DEPLOY` (default off). Demo: `/run-aws-demo`. See [Deploy-then-verify](./concepts/deploy-then-verify).
- **PR endgame closed** — auto-PR → review → merge → re-test, configurable per project (`AIFACTORY_AUTO_PR` / `AIFACTORY_AUTO_MERGE`), with a selectable reviewer (`AIFACTORY_PR_REVIEWER` = `aifactory` | `copilot` | `any`) and a bounded auto-feedback fix-and-re-review loop (≤2 cycles) on changes-requested. See [`guides/pr-endgame.md`](https://github.com/olafkfreund/AIFactory/blob/dev/guides/pr-endgame.md).
- **Contract-carrying TFactory handoff** — a signed Task Contract v2 (RFC-0002) carries declared acceptance criteria, lanes, and endpoints through to TFactory, so it verifies the declared profile instead of inferring it. See [Task Contract](./task-contract).
- **Opt-in direct-API-key auth** — `AIFACTORY_ALLOW_API_KEY=1` for operators billing via a direct key; OAuth-only and key-scrubbed by default. See [API-key auth](./concepts/api-key-auth).
- **Test authenticated web apps** — deploy behind a login, browser-test logs in as a test user, records screenshots + findings as proof. See [`techdocs/testing-authenticated-web-apps.md`](https://github.com/olafkfreund/Factory/blob/main/techdocs/testing-authenticated-web-apps.md).
- **Multi-agent Live Console grid** — stream every active agent's console for a project at `/console/:projectId`. See [Live Console](./concepts/rmux-live-console).

### May 2026 — Mission Control + Enterprise v1.1 + delegation

- **Mission Control workspace + portal UX** (PR #311) — a full-page three-pane task workspace (plan & subtasks · live activity + embedded console · preview / files / review), opened on demand from the task header. Ships with a portal polish pass: skeleton loaders, a live progress timeline, a theme-aware terminal, a "waiting for you" review beacon, and animated streaming logs. See [Mission Control workspace](./concepts/mission-control-workspace).
- **Epic #35 — Enterprise v1.1** **9/9 children shipped (2026-05-28)**:
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

- **Epic #92 — Delegation (Copilot + Duo)** — hand the coder phase off to GitHub Copilot or GitLab Duo Workflow while AIFactory keeps the planning + governance. See the [Delegation concept page](./concepts/delegation).
- **Epic #82 — Portal-managed Git clones** — clone repos into the portal's workspace root (env-aware on laptop vs Helm PVC), with encrypted-at-rest stored credentials for private repos. See [Portal-managed clones](./concepts/portal-clones).

## Next

Open work, as of November 2026. None of it is released:

- Spec creation as a Kubernetes Job, behind `AIFACTORY_SPEC_BACKEND`
  ([#2026](https://github.com/olafkfreund/AIFactory/issues/2026)).
- Per-org GitHub App installation tokens
  ([#2039](https://github.com/olafkfreund/AIFactory/issues/2039)).
- Scoped `acw_` keys on WebSocket and org-filtered `/ws/events`
  ([#2017](https://github.com/olafkfreund/AIFactory/issues/2017),
  [#2027](https://github.com/olafkfreund/AIFactory/issues/2027)).
- First-class Linear integration: bidirectional sync (today is one-way GitHub import only).
- Algolia DocSearch on this docs site.

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
