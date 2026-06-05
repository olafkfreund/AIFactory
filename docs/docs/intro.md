---
slug: /intro
title: AIFactory
sidebar_position: 1
---

# AIFactory — the open-source AI software engineer you can self-host and audit

**AIFactory** is an open-source, self-hostable platform that turns a task into shipping code through a pipeline you can watch and verify: **spec → plan → code → QA**, with human-review gates at each step.

Most AI coding tools ask you to either (a) ship your source to someone else's cloud, or (b) trust an unsupervised agent's diff. If you work somewhere that can't do either — a bank, a hospital, a government team — you've been stuck. AIFactory is built for exactly that gap.

You bring an issue (or a one-line task). AIFactory's planner agent produces a tight spec and an implementation plan; you review it. A coder agent (Claude, OpenAI, Gemini, Codex, or a local Ollama / OpenAI-compatible model — your call per phase) works inside an isolated git worktree. A QA reviewer agent validates the output against the spec's acceptance criteria. Every action lands in a hash-chained audit log. You merge when you're happy — and you can prove, afterwards, exactly what happened.

You watch the whole thing happen live in the **Agent Console** — read-only by default, one-click Attach when you want to drive.

> **New to the project?** Start with [Why AIFactory](./why-aifactory) for the problem we're solving and the principles behind it.

## Who it's for

- **Platform & security engineers in regulated orgs** (banking, healthcare, government, defense) who need an autonomous coding capability they can run *inside their own perimeter* — self-hosted, SSO, audit trail, isolation — without failing the next audit.
- **Self-hosters and homelab/platform teams** who want to run an AI software engineer on their own infrastructure, against their own choice of model (including fully local), with no data leaving the network.
- **Solo developers and small teams** who want an autonomous-but-supervisable pipeline for routine work — and a written spec, plan, diff, and QA pass they can actually review instead of a black box.

## What makes it different

- **Self-hosted, in your perimeter.** Runs on your own Kubernetes via the Helm chart (or docker-compose on a laptop). Your code never has to leave your network.
- **Spec-first, not vibe-first.** Every run starts from a written spec with acceptance criteria. You approve the plan before code is written and the diff before it merges.
- **Auditable by design.** Every action is journaled in a hash-chained audit log; every spec, plan, and QA report is on disk and in version control. SOC2 / ISO evidence in the enterprise build.
- **No vendor lock-in.** Pick a model per phase — Claude, OpenAI, Gemini, Codex, or a local Ollama / OpenAI-compatible endpoint. You own your model bill.
- **Isolated by default.** Each task runs in its own git worktree on its own branch. Nothing touches your working tree until you merge.
- **MCP control plane.** 27 MCP tools across stdio + HTTP+SSE transports let any MCP-aware editor (Claude Code, Cursor, Continue.dev) inspect and direct AIFactory tasks, including autonomous hand-off via the `/handover` skill.

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
