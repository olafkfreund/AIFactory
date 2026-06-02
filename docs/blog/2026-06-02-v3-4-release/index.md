---
slug: aifactory-3-4-observability-messaging-solo-mode
title: "AIFactory 3.4: watch the build, message the agent, and a token-saving solo mode"
authors: [olaf]
tags: [release, observability, agents, providers]
description: AIFactory 3.4 adds per-task observability panels, inter-agent messaging with @agent/#task mentions, a single-agent solo mode for small jobs, and an Antigravity CLI provider — plus a round of provider and worktree fixes.
---

The 3.4 line is about **seeing and steering** a build while it runs — and trimming the overhead when a job is small. Here's what landed.

{/* truncate */}

## See where the build is going: observability panels

Every task now has an **Observability** tab with three live panels:

- **Token usage** — total tokens vs the context window, total cost, and a per-category breakdown (system prompt / user messages / tool outputs / thinking). An over-large system prompt or a runaway tool-output category is now obvious at a glance, and attribution survives a context compaction instead of resetting.
- **Resource usage** — live CPU and memory for the agent's own process, polled every few seconds. This is how you tell a working build from a hung one without tailing logs.
- **Send to agent** — a box to drop a message straight into the agent's inbox.

See [Task observability panels](/concepts/observability-panels).

## Steer it: agent messaging & mentions

Builds are mostly sequential (plan → code → QA → fix), but some coordination has to happen *mid-run*. 3.4 adds a file-backed **inbox** with **between-turn delivery**: messages are picked up at the next turn boundary (atomic writes, read-back-verified), so you — or another agent — can nudge a running build without restarting the session.

Message text is parsed for **`@agent`** and **`#task`** mentions, with the boring-but-important details handled: mentions inside inline code are ignored, and `someone@example.com` is never mistaken for a mention. See [Agent messaging & mentions](/concepts/agent-messaging).

## Trim the overhead: solo mode

For a one-line fix or a throwaway script, the full four-session pipeline is overkill. **Solo mode** collapses it to a single self-directed agent that writes its own plan and works it in the same loop — far fewer tokens and sessions. It's **opt-in** (env var, per-task flag, or global config) and off by default, because there's no independent QA pass; keep the full pipeline for anything you'd want reviewed. See [Solo mode](/concepts/solo-mode).

## Providers

- **Antigravity CLI** — the Gemini provider, renamed, with **portal-driven install/update** (Settings → CLI Tools) so you can pull a new version without leaving the UI. The isolated worktree is trusted automatically so the CLI can edit files.
- **OpenCode CLI** — a community/self-host runtime, with an automatic workaround for the sandbox model-catalogue problem. See the [multi-provider](/concepts/multi-provider) page for the tier note and caveats.
- Plus fixes: Codex builds on ChatGPT-account logins (account-default model, no forced `--model`), and openai-compatible Bedrock/Azure/Vertex routing through the LiteLLM gateway.

## Fixes worth calling out

- **Worktree `core.bare` self-heal** — a stray `core.bare=true` on the primary checkout used to break every git operation; the worktree manager now heals it on init and guards each call.
- **Project-context reconciliation** — the kanban board, sidebar, and project dropdown no longer drift apart on load (the dropdown could previously get stuck on the wrong project).
- **Honest build state** — all-failed builds now surface as *failed* instead of being quietly labelled "human review".

Container images ship for `linux/amd64`; native `linux/arm64` images are being restored on a dedicated runner.

— As always: provider-agnostic, self-hostable, and the code is right there to read.
