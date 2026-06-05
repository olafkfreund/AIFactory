<!-- This page is seeded from README.md. Edit either file;
     they diverge by design after the initial onboarding. -->

<div align="center">
  <img src="apps/frontend-web/public/logo.png" alt="AIFactory" width="120" />

# AIFactory

**Spec-Driven Development for AI agents — plan, code, ship.**

[![CI](https://github.com/olafkfreund/AIFactory/actions/workflows/ci.yml/badge.svg?branch=dev)](https://github.com/olafkfreund/AIFactory/actions/workflows/ci.yml)
[![Docs](https://img.shields.io/badge/docs-aifactory.freundcloud.com-fabd2f)](https://aifactory.freundcloud.com/)
[![License](https://img.shields.io/badge/license-MIT%20OR%20GPL--3.0-blue.svg)](LICENSE)
[![Node 24+](https://img.shields.io/badge/node-24%2B-green)](https://nodejs.org/)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)](https://www.python.org/)

[Docs](https://aifactory.freundcloud.com/) ·
[Demo](https://aifactory.freundcloud.com/demo) ·
[Architecture](https://aifactory.freundcloud.com/architecture/overview) ·
[Roadmap](https://aifactory.freundcloud.com/roadmap) ·
[Contributing](https://aifactory.freundcloud.com/contributing)

**📖 New here? Start at the docs → [aifactory.freundcloud.com](https://aifactory.freundcloud.com)** — guided demo, screenshots, architecture, and the full getting-started guide.

</div>

---

## What it is

AIFactory turns GitHub issues into shipping code via a coordinated **planner / coder / QA** agent pipeline. You bring an issue (or a one-line task description) — AIFactory writes a spec, plans the work, codes it in an isolated git worktree, validates against the spec's acceptance criteria, and hands you back a merge-ready branch.

You watch the whole thing happen live in the **Agent Console** — read-only by default, one-click Attach when you want to drive — or open the task in **Mission Control**, a full-page three-pane workspace (plan · live activity + console · preview / files / review). See [`docs/docs/concepts/mission-control-workspace.md`](docs/docs/concepts/mission-control-workspace.md).

> **🚀 What's new (May 2026)** — three more epics closed on top of the May MCP shipset:
>
> - **Delegation (#92)** — hand the coder phase off to **GitHub Copilot Coding Agent** or **GitLab Duo Workflow** while AIFactory keeps the planning + governance. Hybrid only: planner runs on Claude, the structured plan lands as a comment on the issue, then the provider's agent codes. See [`docs/docs/concepts/delegation.md`](docs/docs/concepts/delegation.md).
> - **Portal-managed Git clones (#82)** — point the portal at a Git URL, it clones into a workspace root (laptop default `~/.aifactory/workspaces/`, Helm-templated PVC on K8s). Stored Personal Access Tokens encrypted at rest. Required for SaaS / Kubernetes deployments. See [`docs/docs/concepts/portal-clones.md`](docs/docs/concepts/portal-clones.md).
> - **Scoped MCP API keys (#154)** — replace the host-wide admin token at `~/.aifactory/.token` with per-developer scope-gated `acw_` keys. Mint via **Settings → API Keys**, drop in `$AIFACTORY_MCP_KEY`, done. Legacy admin token still works as a wildcard fallback. See [`docs/docs/concepts/mcp-stdio-keys.md`](docs/docs/concepts/mcp-stdio-keys.md).
>
> Earlier May 2026 shipset (still current): **27 MCP tools** across stdio + remote HTTP+SSE transports, the **`/handover` skill** for Claude Code, **default MCP servers** that auto-enable per project, and **Remote Control** wiring for `claude.ai/code` on any device.
>
> See [`guides/HANDOVER_WORKFLOW.md`](guides/HANDOVER_WORKFLOW.md) for the developer flow, [`guides/CLAUDE_CODE_MCP_TOOLS.md`](guides/CLAUDE_CODE_MCP_TOOLS.md) for the stdio tool catalog, and [`guides/REMOTE_MCP_SERVER.md`](guides/REMOTE_MCP_SERVER.md) for the HTTP+SSE server (Cursor / Continue.dev / non-Claude clients).

## Enterprise v1.1 (May 28, 2026 — Epic #35)

AIFactory ships **7 major enterprise features** — multi-tenant isolation, observability, audit hardening, and IdP integration for regulated deployments. All features are opt-in via Helm values; default deployments remain unchanged.

| Capability | Issue | Concept doc |
|-----------|-------|-------------|
| **SAML 2.0 + SCIM 2.0** — Legacy IdP federation (ADFS-era banks, Azure AD provisioning) | #41 | [saml-scim](docs/docs/concepts/saml-scim.md) |
| **Tenant Isolation Mode** — Per-tenant K8s namespace + NetPol + S3 + Vault + leader election | #36 | [tenant-isolation](docs/docs/concepts/tenant-isolation.md) |
| **LiteLLM Gateway** — Per-org budget + rate-limit + allowlist + PII-redacted audit log | #38 | [litellm-gateway](docs/docs/concepts/litellm-gateway.md) |
| **Bedrock + Vertex Routing** — Cloud-provider LLMs (AWS, Google) via LiteLLM | #39 | [cloud-llm-routing](docs/docs/concepts/cloud-llm-routing.md) |
| **Signed Audit-Chain Anchor** — Daily HMAC-anchored chain for ISO 27001 A.12 compliance | #43 | [audit-anchor](docs/docs/concepts/audit-anchor.md) |
| **OpenTelemetry Distributed Tracing** — W3C `traceparent` across web + agent + subprocess | #42 | [observability-tracing](docs/docs/concepts/observability-tracing.md) |
| **gVisor Sandbox** — Agent pods opt-in to gVisor RuntimeClass for kernel-level isolation | #37 | — |

**Multi-replica support:** S3 workspace storage + Redis pub/sub (#40) enable horizontally scaled deployments.

**What's next:** v1.2 roadmap includes Claude-on-LiteLLM enforcement wrapper, per-tenant audit anchors, and SAML Single Logout — tracked in Epic #204.

## Why it's different

- **Spec-first, not vibe-first.** Every agent run starts from a written, reviewable spec with acceptance criteria. Plans are editable before code is written.
- **Multi-provider by design.** Pick a model per phase. Plan with Claude Opus, code with a cheap local Ollama qwen3, validate with Sonnet. Anthropic / OpenAI / Ollama / Gemini / Codex / any OpenAI-compatible endpoint.
- **MCP control plane.** 27 tools across two transports let any MCP-aware editor inspect and direct AIFactory. The `/handover` skill turns "this is bigger than I thought" into an autonomous overnight run with one keystroke. **Scope-gated per-developer keys** in v1.1 mean shared hosts and SaaS deployments don't hand out admin power.
- **Provider-agent delegation.** On GitHub repos, AIFactory can hand the coder phase off to **Copilot Coding Agent**; on GitLab, to **Duo Workflow**. AIFactory still authors the spec + plan; the provider's agent does the typing. Cuts Claude spend ~10× for delegated tasks.
- **Portal-managed clones.** Point AIFactory at a Git URL and it clones into a workspace PVC (on K8s) or a configurable workspace root (on laptops). Stored PATs are encrypted at rest. Required for SaaS / Kubernetes installs.
- **Infra-aware out of the box.** A catalog of default MCP servers (Kubernetes, AWS, Azure, GitHub) auto-enables per project when markers + credentials line up. Read-only by default, audit-logged, CVE-aware version pins.
- **One screen to drive a task.** Mission Control puts the plan, the agent's live activity + embedded terminal, and the output (running preview, diff, or merge controls) side by side — no tab-switching while a build runs.
- **Isolated by default.** Each task runs in its own git worktree. Nothing touches your working tree until you merge.
- **Auditable.** Hash-chained audit log, on-disk specs+plans+QA reports, full SOC2 evidence catalog in the enterprise build.

## Quickstart (60 seconds)

```bash
git clone https://github.com/olafkfreund/AIFactory
cd AIFactory
npm run install:all
claude setup-token   # paste into apps/backend/.env as CLAUDE_CODE_OAUTH_TOKEN
```

Start the two servers (in separate terminals):

```bash
cd apps/web-server  && python -m server.main         # :3101
cd apps/frontend-web && npm run dev                  # :3100
```

Open <http://localhost:3100> and create your first project.

Full installation guide: **[Getting Started →](https://aifactory.freundcloud.com/getting-started)**

## See it work

<p align="center">
  <img src="docs/static/img/handover-workflow.gif" alt="Terminal walkthrough — clone the demo repo, file a GitHub issue, type /handover in Claude Code, watch AIFactory's planner / coder / QA pipeline produce a merge-ready branch" width="720" />
</p>

A 45-second terminal walkthrough of the `/handover` workflow: clone the demo repo, file a GitHub issue, type `/handover` in Claude Code, AIFactory's planner → coder → QA pipeline lands at a merge-ready branch. Every artifact shown was produced by a real agent run — the recording compresses the timeline.

The repo also ships a scripted end-to-end demo that exercises the whole pipeline against a public sample repo:

```bash
./scripts/demo.sh
```

It seeds 3 GitHub issues, registers the demo repo with your portal, imports the issues as backlog tasks, prompts you to drive Claude Code from the terminal, then kicks off an autonomous build — all in about 90 seconds. Pass `--yolo` to skip the Enter-prompts between steps.

Walkthrough with screenshots + browser-side video: **[Demo →](https://aifactory.freundcloud.com/demo)**

## Screenshots

<table>
  <tr>
    <td><img src="docs/static/img/screenshots/03-kanban.png" alt="Kanban board" /></td>
    <td><img src="docs/static/img/screenshots/09-live-agent-console.png" alt="Live Agent Console" /></td>
  </tr>
  <tr>
    <td><img src="docs/static/img/screenshots/06-task-detail-plan.png" alt="Plan review" /></td>
    <td><img src="docs/static/img/screenshots/12-settings-llm-providers.png" alt="LLM provider settings" /></td>
  </tr>
  <tr>
    <td colspan="2"><img src="docs/static/img/screenshots/13-settings-mcp-servers.png" alt="Settings → MCP Servers tab — the new default-MCP-server catalog (Epic #100) showing per-project auto-enable status" /></td>
  </tr>
</table>

> Screenshots are auto-captured by `scripts/capture-screenshots.ts` — refresh them with `npm -w apps/frontend-web run capture-screenshots`.

## Documentation

The full documentation lives at **<https://aifactory.freundcloud.com/>**:

- **[Getting Started](https://aifactory.freundcloud.com/getting-started)** — install + first task
- **[Demo](https://aifactory.freundcloud.com/demo)** — guided end-to-end walkthrough
- **[Concepts](https://aifactory.freundcloud.com/concepts/spec-driven-development)** — spec-driven development, multi-provider routing, the rmux Live Console
- **[Architecture](https://aifactory.freundcloud.com/architecture/overview)** — agents, data flow, security model, Mermaid diagrams
- **[Wiki](https://aifactory.freundcloud.com/wiki/faq)** — FAQ, troubleshooting, glossary
- **[Compliance](https://aifactory.freundcloud.com/compliance/soc2)** — SOC 2 evidence, GDPR, encryption-at-rest

Legacy guides (pre-2026-05-26 rewrite) are archived under [`docs-archive/2026-05-26/`](docs-archive/2026-05-26/) and remain searchable in git history.

## Stack

- **Frontend** — React 19 + Vite + xterm.js + Tailwind v4
- **Web Server** — FastAPI + WebSocket, Postgres + Alembic migrations
- **Agent Runtime** — Python 3.12, Claude Agent SDK, provider abstraction over Anthropic / OpenAI / Ollama / Gemini / Codex
- **Deploy** — Helm chart (`charts/aifactory/`), distroless cosign-signed images, OIDC SSO, KMS-backed encryption at rest

## Contributing

Branching: `dev` is the working branch. Branch from `origin/dev`, sign your commits (`git commit -s`), open PRs against `dev`. `main` is a release branch and only receives promotion merges.

```bash
git fetch origin
git checkout -b feat/my-feature origin/dev
git push -u origin feat/my-feature
gh pr create --base dev
```

CI runs ruff + pytest + frontend typecheck + Postgres acceptance + multiple compliance gates on every PR. Full guide: **[Contributing →](https://aifactory.freundcloud.com/contributing)**

## License

Dual-licensed under **MIT** or **GPL-3.0** at your option — see [LICENSE](LICENSE), [LICENSE-MIT](LICENSE-MIT), and [LICENSE-GPL](LICENSE-GPL).

## Acknowledgements

Built with the [Claude Agent SDK](https://docs.anthropic.com/claude/docs/claude-agent-sdk), [FastAPI](https://fastapi.tiangolo.com/), [Docusaurus](https://docusaurus.io/), and [rmux](https://github.com/Helvesec/rmux) (terminal multiplexer fork in Rust, used for the Live Console).
