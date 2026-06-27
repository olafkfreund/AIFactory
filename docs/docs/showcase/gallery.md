---
title: Screenshot gallery
sidebar_position: 5
slug: /showcase/gallery
description: A curated tour of the AIFactory portal — every main view, the key dialogs, and real tasks (including a failed run), captured from the live deployment.
---

# Screenshot gallery

A curated tour of the AIFactory portal, captured from the live deployment at
`aifactory.freundcloud.org.uk`. These are real projects and real tasks — both a
successful run and one that ended in errors — not mock-ups.

> Refresh these with `npm -w apps/frontend-web run capture-screenshots`
> (drives the live portal via Playwright; see `scripts/capture-screenshots.ts`).

## Getting in

The portal authenticates with an API token, or with single sign-on via your
organisation's identity provider.

![API-token login screen, with the SSO option below it](/img/screenshots/01-login.png)

## The board

The kanban board lays a project's tasks across the four PARR lanes — **Plan**
(PFactory), **Code** (AIFactory), **Review** (TFactory) and **Done** (shipped).
Each card carries its phase, a progress bar, and the Plan / Code / Validate / Log
controls.

![Kanban board showing tasks across the Plan, Code, Review and Done lanes](/img/screenshots/02-kanban-board.png)

Creating a task is a single dialog: describe what you want in natural language
(you can `@`-reference files), optionally name it, and the planner takes it from
there.

![Create New Task dialog](/img/screenshots/03-task-create.png)

## A task, in detail

Opening a card gives you the full task: the spec on the **Overview** tab, the
planner's **Subtasks**, streamed **Logs**, the changed **Files**, and an
**Observability** tab.

![Task detail — Overview tab with the generated spec](/img/screenshots/04-task-detail-overview.png)

The Subtasks tab is the implementation plan — file references and per-step
status, reviewable and editable before any code is written.

![Task detail — Subtasks / plan tab](/img/screenshots/05-task-detail-subtasks.png)

Logs stream the agent's work as it happens.

![Task detail — Logs tab](/img/screenshots/06-task-detail-logs.png)

Files shows what the run actually changed.

![Task detail — Files tab](/img/screenshots/07-task-detail-files.png)

Observability breaks token usage down by category — system prompt, user
messages, team/coordination context, tool outputs, thinking + assistant output —
each with its own cost, plus resource usage and a direct "send to agent" channel.

![Task detail — Observability tab with per-category token usage and cost](/img/screenshots/08-task-detail-observability.png)

## When a run goes wrong

Not every run succeeds, and the board doesn't hide it. Tasks that hit problems
carry a **Has Errors** badge and a failed progress bar.

![A project board showing tasks in a Has Errors / failed state](/img/screenshots/09-failed-task-board.png)

Open one and you get the same detail surface — here a task sitting at the Human
Review gate with errors flagged, the plan-review panel, and a Request Changes box
to hand corrections back to the agent.

![Detail of a task flagged with errors, at the Human Review gate](/img/screenshots/10-failed-task-detail.png)

## Beyond the board

The portal is more than a kanban. The left rail carries the workspace tools.

The **Files** browser exposes the task's workspace.

![Workspace Files browser](/img/screenshots/12-files.png)

**Agent Terminals** spawn multiple terminals to run agents in parallel.

![Agent Terminals view](/img/screenshots/14-terminal.png)

**MCP** lists the Model Context Protocol servers available to agents.

![MCP servers view](/img/screenshots/15-mcp.png)

**Worktrees** tracks the git worktrees the build uses.

![Worktrees view](/img/screenshots/17-worktrees.png)

**GitHub Issues** syncs issues straight into the board.

![GitHub Issues sync view](/img/screenshots/19-github-issues.png)

## Settings and providers

Settings carries appearance, the default agent profile and model, integrations,
git credentials, scoped API keys, and more.

The **Agent Settings** pane sets the default model and agent framework, with
presets for complex tasks, balanced work, and quick edits.

![Settings — Agent Settings, default model and agent profile](/img/screenshots/21-settings-agent.png)

**LLM Providers** is where multi-provider lives: add multiple Claude
subscriptions to auto-rotate on rate limits, connect Codex (OpenAI) and
Antigravity (Google) CLIs, and point at OpenAI-compatible endpoints.

![Settings — LLM Providers, cloud agents and OpenAI-compatible endpoints](/img/screenshots/22-settings-llm-providers.png)

**Integrations** holds third-party service connections and OAuth credentials.

![Settings — Integrations and OAuth credentials](/img/screenshots/23-settings-integrations.png)
