---
slug: the-day-the-waves-went-live
title: "The day the waves went live"
authors: [olaf]
tags: [ai-coding, parallel-execution, reliability, intake, multi-tenancy]
date: 2026-07-17
description: Five releases in one day, 3.6.50 through 3.6.54. A one-line prompt-path bug had left every dispatched build planning without its schema, which is why parallel waves never formed. We fixed the path, ran the first live three-worker wave, kept every merge-back, and clocked 21.4 minutes against a 35-minute serial baseline. Plus idempotent issue intake, tenant scoping, and an auto-PR endgame that finally lands on the right branch.
---

Some days the changelog is one entry. Today it was five releases, 3.6.50
through 3.6.54, and they share a single arc: the machinery for parallel wave
execution had been in the tree for weeks, and today we found out why it had
never fired, fixed it, and watched three workers build one task concurrently
on the live cluster.

{/* truncate */}

## The plans were schema-less all along

Parallel execution needs the planner to say which subtasks are independent.
Our planner prompt asks for exactly that: `parallel_safe` flags and per-subtask
file footprints, so the wave scheduler can put disjoint work in the same wave.

Except the dispatched build Jobs never saw that prompt. The generator loaded it
from a path that did not exist in the packaged layout, and on a missing prompt
it silently substituted a one-sentence fallback. Every Job build had been
planning schema-less. The plans worked, the builds were green, and `--parallel`
quietly produced zero waves, because no plan ever carried the metadata a wave
is made of.

3.6.50 fixes the path to load the real prompt, and changes the failure mode: a
missing prompt is now a hard failure, not a silent downgrade (#920). This is
the same lesson as last week's empty-patch work. The dangerous failure is the
one that looks like success.

## The first live wave, and the bug it caught

With schema-full plans, the next intake build planned a wave: three
independent workers, each in its own isolated sub-worktree, merged back
sequentially. The first live run immediately caught a latent bug. Story-mode
plans put `Story` objects into phases, and the completion check touched an
`is_handoff` attribute that `Story` promised but never had. The crash killed
mark-complete and merge-back for every worker: two of the three workers'
finished outputs were discarded and redone serially, cutting the roughly 3x
wave speedup to about 1.6x.

3.6.52 gives `Story` the property it promised (#930). The re-run is the number
we care about: three concurrent workers, all three merge-backs kept, wall
clock 21.4 minutes against a roughly 35-minute serial baseline for the same
task. The wave path is no longer a feature that exists in the code. It is a
feature that has run.

Along the way, 3.6.51 closed the remaining gaps between what you ask for and
what a dispatched Job actually receives: quick mode, selected skills, and an
explicit `base_branch` override are now all threaded into the Job (#916). The
previous behaviour dropped them silently and only looked correct when the
defaults happened to match.

## Intake grows up

Three smaller changes make the issue-driven intake path safer to point at real
repositories:

- **Idempotent from-issue intake** (#878). Redelivery of the same GitHub issue
  returns the existing task with `deduplicated: true` instead of creating a
  duplicate spec and a duplicate build, keyed on the provenance issue number
  that intake already stamps.
- **Auto-close on merge.** A CI workflow now closes issues whose fix reached
  main, driven by PR-body keywords, so fixed issues stop lingering open just
  because the fix merged through an integration branch first.
- **Tenant scoping** (#925). Behind the `AIFACTORY_MULTI_TENANT` flag, every
  task and project is stamped with a tenant resolved from the `X-Tenant-Id`
  header, list endpoints filter by it, and the TFactory handoff carries it.
  Flag off is byte-for-byte unchanged behaviour. This is the first
  application-level piece of the fleet multi-tenancy program.

## The endgame lands on the right branch

The auto-PR endgame had a habit that made dogfooding awkward: it defaulted to
main whenever the requirements named the repo, even for repositories that
integrate on dev. 3.6.54 threads a `base_branch` field from the intake
configuration through from-issue and task metadata all the way to PR creation,
and the auto-PR body now carries `Fixes #N` for the origin issue (#936). A
build that started from a labelled issue now ends as a PR against the branch
the repo actually integrates on, linked back to the issue that asked for it,
and the new auto-close workflow retires that issue when the fix reaches main.
The loop closes without a human touching it.

## What this adds up to

The thread through all five releases is the same one as last cycle: silent
downgrades are the enemy. A prompt loader that falls back to a stub, a Job
dispatcher that drops flags it does not recognise, an intake route that
happily creates the same task twice. None of them fail loudly, all of them
cost you later. Today's releases replace each of those with either correct
behaviour or a hard, visible error.

The operator-facing details, the intake repo configuration with
`base_branch`, the `factory:*` label taxonomy, the parallel precedence rules,
and the tenancy flag, are documented in the
[configuration reference](/configuration-reference).
