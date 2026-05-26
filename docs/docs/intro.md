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
- **Isolated by default.** Each task runs in its own git worktree on its own branch. Nothing touches your working tree until you merge.
- **Auditable.** Every action is journaled in a hash-chained audit log. Every spec, plan, and QA report is on disk and in version control.

## Get started

- **[Install in 60 seconds →](./getting-started)**
- **[Watch the demo →](./demo)**
- **[Read the architecture →](./architecture/overview)**
