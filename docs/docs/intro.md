---
slug: /
title: AIFactory
sidebar_position: 1
---

# AIFactory — Spec-Driven Development for AI agents

**AIFactory** is a web-based platform that turns GitHub issues into shipping code via a coordinated planner / coder / QA agent pipeline.

You bring an issue (or a one-line task description). AIFactory's planner agent — driven by Claude — produces a tight spec and an implementation plan, then hands the plan to a coder agent (Claude, Ollama, OpenAI, Codex, Gemini — your call per phase) that works inside an isolated git worktree. A QA reviewer agent validates the output against the spec's acceptance criteria. You merge when you're happy.

You watch the whole thing happen live in the **Agent Console** — read-only by default, one-click Attach when you want to drive.

## Who it's for

- **Solo developers** who want an autonomous-but-supervisable pipeline for routine work (CRUD endpoints, refactors, doc updates, test scaffolding).
- **Small teams** that want to triage GitHub issues into agent-shippable units without writing planning prompts by hand.
- **Enterprise teams** that need spec-first AI development with SOC2 evidence catalogs, OIDC SSO, encrypted-at-rest secrets, and self-hosted Helm deployment.

## What makes it different

- **Spec-first, not vibe-first.** Every agent run starts from a written spec with acceptance criteria. Plans are reviewable and editable before code is written.
- **Multi-provider by design.** Pick a model per phase. Plan with Claude Opus, code with a cheap local Ollama qwen3, validate with Sonnet. No vendor lock-in.
- **MCP control plane.** 27 MCP tools across stdio + HTTP+SSE transports let any MCP-aware editor (Claude Code, Cursor, Continue.dev) inspect and direct AIFactory tasks. The `/handover` skill turns "this is bigger than I thought" into an autonomous overnight run with one keystroke.
- **Infra-aware out of the box.** A catalog of default MCP servers (Kubernetes, AWS, Azure, GitHub, with GitLab + Azure DevOps next) auto-enables per project when infra markers AND credentials line up. Agents can see and reason about the cloud they're operating in, without per-task setup.
- **Isolated by default.** Each task runs in its own git worktree on its own branch. Nothing touches your working tree until you merge.
- **Auditable.** Every action is journaled in a hash-chained audit log. Every spec, plan, and QA report is on disk and in version control.

## Recently shipped (May 2026)

The MCP Control-Plane Epic (#50) and Default MCP Servers Epic (#100) landed together — a substantial expansion of how developers interact with the platform:

- **Stdio MCP server** at `apps/backend/mcp_server/aifactory_server.py` exposes 15 task-control tools (`task_list`, `task_create_and_run`, `task_approve_plan`, etc.) to any Claude Code session opening this repo via the project-scoped `.mcp.json`.
- **Remote HTTP+SSE MCP server** at `/api/mcp-remote/sse` (opt-in via `AIFACTORY_MCP_REMOTE_ENABLED=true`) exposes 12 tools to non-Claude clients with `acw_` API key + scope-gating (`mcp:read` / `mcp:write`).
- **`/handover` skill** for Claude Code in this repo — captures conversation context, calls the MCP `task_create_and_run` primitive, returns a portal URL. See [`guides/HANDOVER_WORKFLOW.md`](https://github.com/olafkfreund/AIFactory/blob/main/guides/HANDOVER_WORKFLOW.md).
- **Default MCP server catalog** (Kubernetes, AWS, Azure, GitHub) auto-enables based on project markers + credential probes. Read-only by default. CVE-2026-46519-aware pinning.
- **Remote Control** integration with Claude Code's native `--remote-control` flag — drive a running AIFactory agent from `claude.ai/code` on any device. See [Remote Control](./concepts/remote-control).

## Get started

- **[Install in 60 seconds →](./getting-started)**
- **[Watch the demo →](./demo)**
- **[Read the architecture →](./architecture/overview)**
- **[Try `/handover` →](https://github.com/olafkfreund/AIFactory/blob/main/guides/HANDOVER_WORKFLOW.md)**
