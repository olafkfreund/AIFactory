---
slug: build-on-the-latest-models
title: "Building on the latest models — with Copilot joining the coders"
authors: [olaf]
tags: [ai-coding, models, claude, copilot, unified-shell]
date: 2026-07-07
description: AIFactory now defaults to Claude Opus 4.8, adds GitHub Copilot as a coding runtime, and joins the fleet-wide unified shell. A one-page showcase is ready to download.
---

AIFactory builds software autonomously: it takes a plan, writes the code in an
isolated git worktree, and opens a pull request. This round of work moved it onto
the current models, added a third coding runtime, and connected it to the rest of
the family.

{/* truncate */}

![AIFactory build workspace](/img/blog/2026-07-07/build-workspace.png)

## On the latest models

The default coder is now **Claude Opus 4.8**, Anthropic's current flagship. Claude
**Sonnet 5** and the current OpenAI Codex, Google Gemini, and GitHub Copilot models
are all selectable per stage. We refreshed the whole model catalogue in one pass —
and fixed a stale price entry that had been over-stating Opus cost three-fold.

## Three coding runtimes

Claude, OpenAI Codex, and **GitHub Copilot** now all run as first-class coding
backends behind one task contract. The Copilot CLI is baked into the build image
and wired as a selectable runtime; turning it fully on is one operator sign-in
away.

## Part of one product

AIFactory also joined the fleet-wide unified shell:

- **A portal switcher** moves you between Plan, Build, Test, and Cockpit.
- **A global command palette** — Cmd-K — searches every portal's work at once.
- **A fleet "needs you" badge** shows how many tasks are waiting on a human.
- **Silent single sign-on** carries you in from any sibling portal.

## Download the showcase

**[AIFactory — one-page showcase (PDF)](/downloads/aifactory-showcase.pdf)**

## The path forward

Next: bring the GitHub Copilot runtime fully live, sharpen acceptance-criteria
fidelity, and keep the build stage able to ship any language to any target.
