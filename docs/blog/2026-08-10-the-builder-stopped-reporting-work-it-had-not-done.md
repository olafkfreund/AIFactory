---
slug: the-builder-stopped-reporting-work-it-had-not-done
title: "The builder stopped reporting work it had not done"
authors: [olaf]
tags: [ai-coding, reliability, compliance, kubernetes, autonomous, observability]
date: 2026-08-10
description: Three weeks on the build side. A reaper that reported writes it never made, tasks that listed as running after their worker died, an Approve button that merged a local branch instead of the pull request, and memory that now pools at project level so it compounds across tasks. Plus the compliance work - PII scrubbed from outbound model calls by default, output-side DLP on agent-authored git output, and scoped service tokens beside the wildcard.
---

AIFactory takes a signed plan and builds it, each task inside its own throwaway
Kubernetes Job. The last three weeks were less about new capability and more
about a category of bug that is specific to autonomous systems: the builder
reporting state that was true of its intention rather than of the world.

{/* truncate */}

## The reaper that reported writes it never made

The clearest example. Tasks whose worker process dies leave rows saying
`in_progress` forever, so the board lists work that stopped hours ago. We built a
reaper to detect and clear them.

The reaper detected them correctly, logged what it was clearing, and did not
write. The log line was emitted next to the update rather than after it, so every
run reported a clean sweep and the rows were still there on the next pass. It had
been running for days, saying the right thing, and changing nothing.

Three fixes came out of that thread: detect orphaned tasks whose worker died,
make the reaper actually write instead of reporting that it did, and sweep
orphans on a schedule rather than only when someone looked. The middle one is the
one worth remembering, because it is invisible in every way except by reading the
rows afterwards.

## Approve merged a local branch, not the pull request

The Approve action in the cockpit merged a local branch. On a normal task this
produced the outcome you expected, so it survived review and use. On a task where
the pull request had moved - a rebase, a review commit, a force push - Approve
merged something that was no longer what the reviewer had read.

Approve now merges the pull request. The distinction never mattered until it did,
and when it does the failure is silent and the reviewer's approval is attached to
a diff nobody merged.

Related: automatic merge is now gated on the review tier, so a change that
requires a higher level of review cannot auto-merge past it.

## A signed plan is not a fresh plan

Trusted plans arrive signed, and the signature proves the plan came from the
planner unaltered. It does not prove the plan is still applicable. A plan signed
against a baseline that has since moved is authentic and stale, and building it
produces a correct implementation of a superseded requirement.

Signed plans are now gated on baseline freshness as well as signature. Both
questions have to be answered, and only one of them was being asked.

## Memory that compounds

Agent memory used to be per-task, which meant every task started from nothing and
learned the same things again. Memory now pools at the **project** level, so what
one task discovers about a codebase is available to the next one.

This is the first phase of a longer piece of work and it is deliberately modest:
pooling is easy to describe and easy to get wrong, and the failure mode - one
task's wrong conclusion contaminating every later task - is worse than no memory
at all. Promotion gates come next.

## The compliance work

A substantial block of this period went into the compliance program, and the
build side owns the parts that touch data leaving the system.

- **PII is scrubbed from outbound model calls by default.** Redaction used to be
  opt-in and applied only to the audit row, which meant the audit trail was clean
  and the provider still received the data. Default-on, on the outbound path, is
  the only version of that control that does anything.
- **Output-side DLP scans agent-authored git output.** The input side had guards;
  the side where an agent writes a file containing something it should not was
  unguarded.
- **Audit events now chain authentication failures, authorization denials and
  gate rejections.** Those are precisely the events an investigation needs and
  they were the ones not in the tamper-evident chain.
- **Scoped service tokens run alongside the wildcard.** The fleet shared one
  token that could do everything, so any single service being compromised meant
  fleet-wide access. Scoped tokens are step one; retiring the wildcard is the
  goal, and it is not done.
- **Access-review evidence is pushed to a drop path** so the review has an
  artifact rather than a recollection.

The honest framing is that this is the beginning of that domain rather than the
end of it. Several of these are step one of several, and the tracking issues say
so.

## Tracing from inside the Job

The build runs in a Job, which is a separate process that knows nothing about the
run that dispatched it. The build Job now emits telemetry from inside itself with
the run's trace context carried across that boundary, so a build is work in the
trace rather than a gap between two spans.

The startup probe needed bounding at the same time. Waiting on the collector
before reporting ready turns an observability dependency into an availability
dependency, which is the wrong trade for a feature whose entire purpose is to
tell you what is happening.

## Permissions the build lane actually needs

A smaller fix with a general lesson: the chart granted the service account a role
that did not match what the build lane actually used. It worked, because
something else in the chain was permissive enough to cover the gap. Granting the
role the lane needs - and no more - is both the security position and the only
way the failure becomes legible when it happens.

## What this adds up to

Every item above is a variation on one question: does the report match what
happened? A reaper that logs a write it did not make, an Approve that merges
something other than what was reviewed, a signature that proves origin but not
freshness, a redaction that cleans the record but not the request.

None of them were reported as bugs, because in each case the system said it was
fine. They were found by reading the artifact instead of the status, which is now
the standard the whole fleet is being held to.
