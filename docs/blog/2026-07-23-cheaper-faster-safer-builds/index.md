---
slug: cheaper-faster-safer-builds
title: "Cheaper, faster, safer builds: four changes under the hood"
authors: [olaf]
tags: [ai-coding, devops, multi-provider, self-hosted]
date: 2026-07-23
description: The last few weeks of AIFactory work were not about new surface area. They were about the build getting cheaper, starting faster, remembering more, and running behind a real boundary. Here is what changed and why it matters.
---

The last few weeks of AIFactory work were not about new features you click. They
were about the build itself getting cheaper to run, faster to start, better at
remembering what it already learned, and safer to let loose on a repo. Four
changes, each small on its own, add up to a build node you can actually afford
to run at scale.

{/* truncate */}

## Every stage no longer pays frontier prices

The obvious way to run a multi-stage agent pipeline is to point every stage at
your best model. It works, and the bill is brutal. Planning, coding, review, and
QA do not all need the same horsepower.

Per-stage cost-aware routing (RFC-0014) sends each stage to an appropriately
sized model tier — `small`, `mid`, or `frontier` — instead of paying frontier
prices everywhere. The safety catch is that the RFC-0011 difficulty tier is
applied as a capability floor: a task graded hard can never be routed below the
model it actually needs, no matter what the tier map says. Every worker in the
completion event is stamped with the tier it ran at, so the routing decision
shows up in the cockpit rather than being taken on faith.

Measured on three identical tasks, the model mix cut cost from 6.48 to 2.91 USD
— a 55 percent reduction — at essentially the same token volume. The saving is
the model mix, not less work.

Non-default runtimes (codex, ollama, agent swarms) stay gated behind an operator
allowlist, `AIFACTORY_RUNTIMES`. Claude is always on; the fan-out runtimes that
multiply spend have to be named explicitly. You opt into cost, you never trip
into it.

## The build stops waiting on npm

The control plane used to npm-install the provider coder CLIs — claude-code,
codex, gemini — into a scratch volume at pod boot. On a slow or hung npm
registry that could run eight minutes and stall the whole rollout, stranding
every in-flight spec behind a package download.

Those CLIs are now baked into the runtime image at build time, pinned, already
on PATH (#791). The control-plane boot never touches npm. A registry having a
bad day is no longer your rollout having a bad day.

## The code graph remembers

AIFactory can hand the coder a Tree-sitter graph of the repository so it
navigates the code instead of grepping blind. Building that graph per task and
throwing it away with the Job was pure waste — the same repository at the same
commit produces the same graph every time.

It is now cached in MinIO, keyed by repo and commit
(`graphify/{repo_slug}/{sha}/graph.json`, #804). An exact hit skips the rebuild;
a miss builds it once and uploads best-effort. Cache errors are always swallowed
and never fail a build — a caching layer that can break your build is worse than
no cache. It stays opt-in behind `AIFACTORY_GRAPHIFY_ENABLED`.

## A real boundary around agent bash

The agent runs bash under a soft command allowlist. An allowlist is a policy,
not a wall. #363 adds the wall: an OS-level bubblewrap sandbox that binds the
task worktree read-write and mounts everything else read-only, so an agent
cannot write outside its worktree or read a host secret even if it wanted to.

It runs unprivileged in-pod, k3d included, and is enabled by default in the
Helm chart. Where bwrap cannot run it degrades to a clean passthrough rather
than failing the build. The two sandbox-escape tests we used to skip — write
outside the worktree, read a host secret — are now live assertions that run in
CI and inside the cluster, proving the boundary rejects both.

## The theme

None of these are demo-day features. They are the difference between a pipeline
that works once and a build node you can run every day without watching the
bill, the clock, or the blast radius. That is the part of the problem that does
not commoditize, and it is where we keep spending the effort.
