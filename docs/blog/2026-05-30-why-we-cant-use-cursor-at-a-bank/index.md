---
slug: why-we-cant-use-cursor-at-a-bank
title: "Why we can't use Cursor at a bank — and what I built instead"
authors: [olaf]
tags: [ai-coding, self-hosted, compliance, governance]
description: AI can write code. For regulated teams the blocker isn't capability — it's trust and provenance. Here's the case for governed, self-hostable, auditable autonomous coding.
---

A friend who works at a bank told me their security team had just banned every cloud AI coding
tool. Not because they're luddites — these are sharp engineers who'd love the productivity. They
banned them because they can't send proprietary source code to a third party, and they can't
explain to an auditor where a given line of code came from. They wanted AI's help. They weren't
allowed to have it.

I kept hearing versions of this. The more I looked, the more I realized it isn't a niche complaint
— it's the unspoken default for a huge slice of the industry. So I built something for it, open
-sourced it, and this post is about why.

{/* truncate */}

## The problem isn't capability. It's trust.

We're past the point of arguing whether AI can write code. It can. The interesting question has
moved: *can you trust what it produced, and can you prove where it came from?*

The data says no, and it's getting worse:

- **96% of developers don't fully trust AI-generated code** — yet only **48% actually verify it**
  (Sonar, 2026). That gap is where bugs and vulnerabilities live.
- For **38% of teams, reviewing AI-written code now takes *more* effort** than reviewing a human's.
  We automated the writing and quietly moved the cost to review.
- **~74% of organizations can't provide security provenance** for AI-generated code. When the
  auditor asks "where did this come from and who approved it?", there's no answer.
- Depending on the study, **40–62% of AI-generated code contains vulnerabilities or design flaws.**

For a solo dev on a side project, fine. For a regulated team, that's a wall.

## Why the cloud tools structurally can't fix this

It's tempting to think the SaaS vendors will just add an "enterprise mode" and the problem goes
away. But the issues are structural, not cosmetic:

- **Data residency.** The tool's value comes from ingesting your repo. If your compliance regime
  says source can't leave the perimeter, "enterprise SSO" doesn't help — the architecture is wrong.
- **No air-gap.** Many regulated environments are network-isolated. A tool that phones home to a
  hosted model service can't run there at all.
- **Opaque actions.** Most agents hand you a diff, not a defensible record of *what they did, in
  what order, and what you approved.*
- **Lock-in.** Betting your whole dev workflow on one vendor's model and pricing is its own risk.

You can't bolt provenance and air-gap onto an architecture designed around "send us your code."

## The thesis: autonomy and governance aren't opposites

Here's the conviction the whole project rests on: **you can have an agent that ships code *and* a
trail you can defend.** Those aren't in tension — you just have to design for both from the start.

Concretely, that means:

- **Spec-first.** Every run begins with a written spec and acceptance criteria — intent you can read
  and edit before anything happens.
- **Review-gated.** You approve the plan before code is written, and the diff before it merges. A QA
  agent checks the result against the spec.
- **Isolated.** Each task runs in its own git worktree. Nothing touches your working tree until you
  decide to merge.
- **Provenance by default.** Every action is journaled in a hash-chained audit log. The spec, the
  plan, and the QA report all live on disk and in version control.
- **Self-hosted.** It runs in *your* perimeter — your Kubernetes cluster, or just docker-compose on
  a laptop — against *your* choice of model, including a fully local one.

## What I built

That's [AIFactory](https://github.com/olafkfreund/AIFactory). It turns a task into shipping code
through a pipeline you watch and verify: **spec → plan → code → QA**, with human-review gates at
each step. You bring your own model — Claude, OpenAI, Gemini, Codex, or a local Ollama /
OpenAI-compatible endpoint — and you own the infrastructure it runs on. Every task lands in a
hash-chained audit log, so afterwards you can show exactly what happened and who approved it.

It's open source (MIT) and I build it solo, full-time. There's a separate enterprise edition for
organizations that need multi-tenant isolation, SAML/SCIM, and signed audit evidence — that's what
funds the open core — but the core pipeline is free, and it's the part most people need.

## If this is your problem too

If you're somewhere that wants AI's productivity but can't use the cloud tools — or you just don't
want to merge code you can't account for — I'd genuinely like to hear what would make this usable
for you. The repo is here: **[github.com/olafkfreund/AIFactory](https://github.com/olafkfreund/AIFactory)**.
Open an issue, or tell me where it falls short.

Autonomy you can't defend isn't worth much in the places that matter most. I think we can do better
than "trust the diff."
