---
title: Why AIFactory
sidebar_position: 2
---

# Why AIFactory

## The problem

AI can write code now. The models are good enough. What's missing isn't capability — it's
**trust and provenance**, and that gap is widest exactly where the stakes are highest.

- 96% of developers don't fully trust AI-generated code — yet only 48% verify it. (Sonar, 2026)
- For 38% of teams, reviewing AI-written code now takes *more* effort than reviewing a human's.
- ~74% of organizations can't produce security provenance for AI-generated code.

For a developer, that means babysitting a black box or cleaning up "vibe-code" you didn't plan.
For a platform or security team at a regulated organization, it's worse: you can't send
proprietary source to a third-party cloud, and you can't explain to an auditor where a given
line of code came from. So the answer becomes "no" — and the productivity stays on the table.

## What we're building instead

AIFactory starts from a simple conviction: **autonomy and governance are not opposites.** You can
have an agent that ships code *and* a trail you can defend.

That conviction shows up as five design commitments:

1. **Self-hosted, in your perimeter.** It runs on your own infrastructure — Kubernetes via the
   Helm chart, or docker-compose on a laptop. Your code never has to leave your network.
2. **Spec-first.** Every run begins with a written spec and acceptance criteria, not a vibe.
3. **Review-gated.** You approve the plan before code is written and the diff before it merges.
   A QA agent checks the work against the spec.
4. **Isolated.** Each task runs in its own git worktree; nothing touches your tree until you merge.
5. **Auditable.** Every action is journaled in a hash-chained audit log; specs, plans, and QA
   reports live on disk and in version control. SOC2 / ISO evidence ships in the enterprise build.

And underneath all of it: **no vendor lock-in.** Route each phase to the model you choose —
Claude, OpenAI, Gemini, Codex, or a local Ollama / OpenAI-compatible endpoint. You own your
model bill, and you're never one provider's pricing change away from a rewrite.

## Where we fit

The autonomous-coding space is crowded at the top with well-funded, cloud-hosted, closed-source
tools. AIFactory deliberately does **not** compete there. Its place is the intersection that those
tools structurally can't occupy:

> A full **spec → plan → code → QA**, review-gated autonomous pipeline, that you can
> **self-host** in your own infrastructure, with a **deep, verifiable audit trail**.

If you can use a cloud SaaS coding agent and you're happy merging unreviewed diffs, there are great
tools for you — use them. AIFactory is for everyone who can't, or won't.

## Open source, and open-core

The core platform — the spec → plan → code → QA pipeline, the web UI, multi-provider routing, git
worktree isolation, single-tenant self-hosting — is **open source and free.** That is the project,
and it's the part most people will ever need.

A separate **enterprise edition** (multi-tenant isolation, SAML/SCIM, signed audit anchors with
SOC2/ISO evidence export, policy packs, and support) exists for organizations that need it. The
revenue from that is what keeps the open-source core healthy and maintained. We'd rather be honest
about that model up front than pretend otherwise.

## Our principles

- **Candor over hype.** We tell you what's production-grade and what's still beta. Regulated teams
  reward honesty, and so does everyone else.
- **Earn trust, don't claim it.** The audit trail, the review gates, and the open code are there so
  you can verify what we say rather than take our word for it.
- **Demand-pulled, not feature-pushed.** We don't add scope that no real user has asked for. If you
  have a use case we don't cover, [open an issue](https://github.com/olafkfreund/AIFactory/issues)
  first.

## Next

- [Getting started](./getting-started) — install and run your first task
- [Spec-driven development](./concepts/spec-driven-development) — the pipeline in depth
- [Architecture](./architecture/overview) — agents, data flow, security model
- [Roadmap](./roadmap) — where this is going, and how decisions get made
