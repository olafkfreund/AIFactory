---
slug: a-coder-that-cannot-fake-a-green-test
title: "A coder that cannot fake a green test"
authors: [olaf]
tags: [ai-coding, reliability, testing, kubernetes, autonomous]
date: 2026-07-19
description: A live end-to-end run on 2026-07-19 drove a plain GitHub issue through plan, build, test, and verdict with no humans in the loop. AIFactory builds every task inside its own throwaway Kubernetes Job and cannot record a passing test unless a real runner actually ran. This is the build side of that run, including the moment the pipeline refused to certify a build with a failing test.
---

Most autonomous coders are trusted on their word. They report a green test suite
and you believe them, because there is no cheap way to check. AIFactory is built
so that word costs nothing and the evidence costs everything: a passing test
checkbox is impossible unless a real test runner actually executed. On
2026-07-19 we drove a plain GitHub issue through the whole pipeline, plan to
verdict, with no humans in the loop, and this is the build side of that run.

{/* truncate */}

## Job-native build

AIFactory does not code in a long-lived worker. Every task builds inside its own
throwaway Kubernetes Job. The Job is created for the task, refreshed to the
current tip of the target repository's main branch, does its work, opens its own
pull request, and is then gone. Nothing carries over between tasks except the
commit and the PR, which is exactly what you want: no accumulated state, no
drift, no shared workspace where one build can quietly poison the next.

The featured task was small on purpose, because a small task makes the machinery
legible. The fleet took an issue asking for a `clamp(value, low, high)` helper on
a demo repository, planned it, built it in an ephemeral Job, and opened
aifactory-demo pull request #387 by itself. A human filed the issue. Everything
after that was the factory.

## The test-evidence gate

Here is the part that matters. Inside the Job, the coder can mark a subtask's
tests as passing only if a real test command actually ran and produced that
result. The gate is tamper-evident: the green checkbox is bound to observed
execution, not to the agent's claim. If no runner ran, there is no pass to
record. An autonomous coder's most tempting shortcut, declaring victory it did
not earn, is simply not reachable from inside the build.

That sounds abstract until it bites, so here is the moment it bit.

## Tests that refuse to lie

Minutes before the clean `clamp` run, the same pipeline built a `slugify`
helper. It compiled, it looked right, and it would have sailed through a coder
that grades its own homework. It failed one of twelve test verdicts on a unicode
edge case. The verification gate did not average that away or wave it through. It
capped the build at VAL-0, the floor, and auto-filed a handback to fix the
defect. It refused to certify a build with a failing test.

That is the capability, not a malfunction. A pipeline that can be honest about
`clamp` passing is only worth trusting if it is equally willing to be honest
about `slugify` failing. The same machinery produced both verdicts on the same
day, and the disappointing one is the one that proves the point.

## Live agent terminals

While a Job runs, its agent streams its terminal into the portal and the cockpit
in real time. You watch the coder read the repository, write the change, and run
the tests as it happens, and with parallel work you watch several agents on one
board at once. There is no separate "what did it do" report to reconstruct after
the fact, because the doing is the record.

## Where the build sits in the run

The build is one stage of the PARR pipeline: PFactory plans against the actual
codebase, AIFactory builds and opens the PR, TFactory generates and runs the
tests in a per-task sandbox and assigns a Verification Assurance Level, and the
verdict threads back onto the PR. For the `clamp` run the verdict was
VAL-1, five of five acceptance criteria met, nine tests generated and kept, none
rejected, a mutation probe killed, confidence 0.96, stable across three runs. The
higher assurance levels reported not_run, correctly, because a pure function has
no API, integration, or browser lane to exercise. An untested dimension is
reported as a gap, never dressed up as a pass.

## The rough edge we are not hiding

The same run surfaced one real gap. The verify verdict is computed correctly, but
its automatic post back onto the pull request is currently gated by a fix we have
tracked as an open issue. We would rather name that than let a clean-looking
walkthrough imply the last mile is done. The factory found the final rough edge
in its own feature and said so, which is the same discipline as the `slugify`
handback pointed at ourselves.

## What this proves

That an autonomous coder can be built so its optimistic failure mode is closed by
construction. A build that cannot fake a green test cannot drift into confident
wrongness, because the one move that would let it, claiming a pass it did not
run, is not available. Every task in its own disposable Job, refreshed to current
main, opening its own PR, with evidence bound to execution: that is a coder you
can leave alone, because when it is wrong it is the first to tell you.

## Watch it run

One continuous walkthrough of all four live portals with this run's own data:

<video controls preload="metadata" src="/video/factory-walkthrough.mp4" style={{width:'100%',maxWidth:'960px',borderRadius:'8px'}} />
