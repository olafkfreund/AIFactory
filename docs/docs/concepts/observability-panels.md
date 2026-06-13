---
title: Task observability panels
sidebar_position: 12
---

# Task observability panels

> *Open any task and see exactly where its context window and money went, what the agent process is doing right now, and a box to message the agent — all on one tab.*

Every task in the portal has an **Observability** tab (Task detail → *Observability*) with three live panels. They answer the three questions you actually ask mid-build: *how much did this cost?*, *is it still working?*, and *can I nudge it?*

## 1. Token usage

A breakdown of the task's context-window spend, by category, with cost:

- A header line — total tokens **/ context window (percent used)** and **total USD cost** — e.g. `3.19M / 200.0k (100%) · $1.88`.
- A stacked bar, then a per-category table: **System / CLAUDE.md / AGENTS.md**, **User messages**, **Team / coordination context**, **Tool outputs**, **Thinking + assistant output** — each with its token count, share of the window, and cost.

This is per-category **token monitoring**: it makes an over-large system prompt or a runaway tool-output category obvious at a glance. The panel also survives context compaction — token attribution is recovered after a compact rather than resetting to zero.

For parallel multi-provider builds, `token_usage.json` also carries a per-worker `workers` map — input/output tokens, cost and duration for **each** worker — so you can see which worker, provider and model spent what, not just the aggregate. The scalar total is preserved unchanged, and the same breakdown is carried on the v1.3 build completion event as `usage.workers[]` / `usage.by_provider` / `usage.by_model`, with a per-worker OTel metrics counterpart (see [Distributed tracing](./observability-tracing.md)). A soft, observe-only budget alert (`usage.budget{limit,spent,exceeded}`) reports overspend but never aborts a build.

Backed by `GET /api/tasks/{id}/token-usage`.

## 2. Resource usage

Live **CPU and memory** for the agent's own process, polled every ~3 seconds:

- When the build is running: PID, CPU %, memory (MB and % of host).
- When it isn't: a plain *"Process is not running."*

This is how you tell a genuinely-working build apart from a hung one without tailing logs. Backed by `GET /api/tasks/{id}/resource-usage`.

## 3. Send to agent

A composer that drops a message into the agent's **inbox** — the same between-turn delivery channel agents use to message each other (see [Agent messaging](./agent-messaging.md)):

- Supports `@agent` and `#task` [mentions](./agent-messaging.md).
- Shows the running message history (or *"No messages sent yet."*).

Backed by `GET` / `POST /api/tasks/{id}/inbox`.

## Why it's on the task, not a global dashboard

Cost, liveness, and steering are all **per-task** questions during a build. Putting them on the task detail (rather than a separate monitoring app) means the context you need to act — the spec, the plan, the logs — is one tab away. For fleet-wide latency/tracing across pods, see [Distributed tracing](./observability-tracing.md) instead; the two are complementary.
