---
slug: parallel-build-executor
title: "70 minutes for a hello-world API: how we taught the build executor to run in parallel"
authors: [olaf]
tags: [ai-coding, devops, multi-provider]
date: 2026-06-06
description: A correct build that takes ~70 minutes and $11 for a simple service isn't a correctness bug — it's a throughput bug. Here's how we made AIFactory run independent subtasks concurrently, kept it auditable, and stopped paying to re-send the same context every turn.
draft: true
---

We built a deliberately boring service to test the pipeline end to end: a small FastAPI API
gateway. AIFactory planned it, implemented it, QA'd it, and the result was genuinely good. It
also took roughly **70 minutes and $11.46** — for something a developer would call a hello-world.

The build wasn't wrong. It was *slow*. And the reason turned out to be embarrassingly simple:
the executor did everything one step at a time, even when the steps had nothing to do with each
other.

![The AIFactory board with the FastAPI gateway PARR demo completed](./board-parr-demo.png)

{/* truncate */}

## The problem: correct, thorough, and serial

That gateway decomposed into 19 subtasks. The executor ran them strictly one-at-a-time — each as
a full agent turn that edits files, runs tools, and self-verifies. Three things made that
expensive:

- **No concurrency, ever.** `app/config.py`, `tests/test_health.py`, and the CI workflow don't
  depend on each other, but they ran in sequence anyway.
- **The speed knobs were a lie.** The API already accepted `parallel: true` and `workers: N`.
  We measured it: turning them on changed *nothing* — subtask timestamps still marched in single
  file. The flags were accepted at the edge and silently dropped before they reached the executor.
- **We paid to re-send context every turn.** The input:output token ratio was about **96:1**.
  Almost all the cost was re-hydrating the same system and repo context on each subtask, not
  generating code.

A second run with `mode: quick, parallel: true, workers: 4` produced *more* subtasks (23) and
took *longer* per subtask. The documented levers genuinely didn't help. That's the bug.

## What we built: dependency-graph waves

The fix is to run independent subtasks **concurrently, in waves**, while keeping the audit trail
intact. The planner now declares, per subtask, the files it will touch and which other subtasks
it depends on. From that the executor builds a dependency graph and schedules waves under three
rules:

1. **Coding is concurrent, but isolated.** Each subtask in a wave runs in its **own git
   worktree**, branched off the task branch. Two agents never share a working tree or git index.
   The scheduler only puts subtasks in the same wave when their file sets are *disjoint*.
2. **Merges are sequential.** When a wave finishes, each child branch is merged back into the task
   branch one at a time — no racing the index.
3. **Plan state is owned by the parent.** Only the orchestrator writes the canonical
   `implementation_plan.json`, and only between waves. The concurrent agents never touch it.

If anything can't be scheduled safely — a dependency cycle, an unknown file footprint, a merge
that fails — the phase falls back to the old serial path for whatever's left. Parallelism is
strictly opt-in (`--parallel`), gated to phases the planner marks `parallel_safe`, and the
serial behaviour is completely unchanged when it's off. It's an accelerator, never a new way to
break a build.

## It's provider-agnostic on purpose

The obvious shortcut would be to lean on Claude's built-in subagents. We didn't, because that
only helps one provider. AIFactory routes work to Claude, Codex, Gemini, Ollama, and any
OpenAI-compatible endpoint — so the parallelism lives *above* the provider layer. A wave can be
four Claude sessions, or four Gemini sessions, or a mix. Every provider gets concurrency for
free, with no vendor lock-in. That's the whole positioning of the project, applied to throughput.

## Memory that survives going parallel

AIFactory keeps a cross-session knowledge graph (Graphiti on an embedded database) so agents
learn from past builds. Running subtasks concurrently put that at risk: four agents writing one
embedded graph at once is a recipe for corruption. So reads stay concurrent — each subtask still
gets its memory context injected — but the actual graph *write* is funnelled through a single
lock. Parallel builds keep capturing what they learn, without the database ever seeing two
writers at once.

## And yes — Claude Opus 4.8

While we were in here, we moved the default Opus tier to **Claude Opus 4.8** across the planner,
coder, and QA roles, and fixed the model picker labels that still said 4.7. One shorthand,
`opus`, now resolves to the current flagship everywhere.

![Agent settings showing Claude Opus 4.8 across phase configuration](./agent-settings-opus-4-8.png)

Memory is configurable in the same settings surface — the embedded knowledge graph, the MCP
endpoint agents read and write through, and the embedding model for semantic search:

![The Memory settings panel — Graphiti, MCP access, and embedding model](./memory-panel.png)

## The part we won't hand-wave: humans, in their language

A governed tool is only useful if the humans in the loop can actually read it. AIFactory's portal
ships fully translated — every label, button, and description goes through translation keys, with
English, French, and Brazilian Portuguese maintained side by side. The same setting reads
naturally wherever you are:

| Key | English | Français | Português (BR) |
|-----|---------|----------|----------------|
| `settings.model` | Model | Modèle | Modelo |
| `settings.memory.title` | Memory | Mémoire | Memória |

There are no hardcoded strings in the UI — new text lands in every locale at once. Autonomy you
can't read isn't autonomy you can govern.

![The portal language picker — English, Français, Português (Brasil)](./language-i18n.png)

## What's still open

We're honest about the scoreboard: the wave scheduler, sub-worktree isolation, merge-back, and
memory integration are built and unit-tested, with the serial path untouched as a safety net. The
number that matters — a clean before/after on that same gateway spec showing at least 2× faster
and 40% cheaper — is the next thing we'll publish, measured on a real run rather than asserted.

If you want governed, auditable, self-hostable autonomous coding without betting on a single
vendor, the work is open source. Come read the executor, file an issue, or break it:
[github.com/olafkfreund/AIFactory](https://github.com/olafkfreund/AIFactory).
