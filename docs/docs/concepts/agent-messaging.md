---
title: Agent messaging & mentions
sidebar_position: 5
---

# Agent messaging & mentions

> *Agents (and you) can drop a message into a running agent's inbox between turns — `@agent` to address someone, `#task` to reference work — without restarting the session.*

AIFactory's pipeline is mostly sequential (plan → code → QA → fix), but some coordination needs to happen *while* an agent is mid-run: a reviewer flags something, you want to redirect the coder, or one agent needs to hand context to another. The **inbox** is that channel.

## The inbox

Each task has a file-backed inbox. Messages are delivered **between agent turns** — the agent picks them up at the next turn boundary rather than mid-thought, so they don't corrupt an in-flight response.

Design points that make it reliable:

- **Between-turn delivery** — fast (no new SDK session needed) and non-destructive to the current turn.
- **Atomic writes** — messages are written to a temp file and `rename`d into place, so a reader never sees a half-written message.
- **Read-back verification** — each message carries a `messageId` that the writer re-reads to confirm delivery.

You can post to the inbox from the portal via the **Send to agent** box on a task's [Observability tab](./observability-panels.md), or agents post to each other's inboxes as part of their work.

Backed by `GET` / `POST /api/tasks/{id}/inbox`.

## Mentions: `@agent` and `#task`

Message and comment text is parsed for structured **mentions**:

- **`@<name>`** — addresses an agent, team, or recipient (e.g. `@coder`, `@qa_reviewer`, `@team-x`).
- **`#<id>`** — references a task.

The parser is deliberately conservative so ordinary prose isn't mangled:

- Mentions **inside inline code** (`` `@example` ``) are ignored.
- **Email addresses** are not mentions — an `@` preceded by a word character (`someone@example.com`) is skipped.

This lets a reviewer write a normal sentence — "`@coder` the retry logic in `#011` drops the last error" — and have the routing happen automatically.

## When to use it

- **You → agent**: nudge or correct a running build without stopping it.
- **Agent → agent**: hand off context or raise a review obligation mid-pipeline.
- **Not** a replacement for the spec: durable requirements still belong in the spec/plan; the inbox is for *in-flight* coordination.
