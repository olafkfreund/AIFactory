---
slug: the-week-we-stopped-failing-silently
title: "The week we stopped failing silently"
authors: [olaf]
tags: [ai-coding, reliability, cost, benchmarks, multi-provider]
date: 2026-07-13
description: A July 2026 cycle recap. An external SWE-bench baseline showed that almost half of AIFactory's failures were not wrong answers but empty patches — silent no-ops. We root-caused and fixed the dominant cause three ways, killed a second latent no-op, then shipped per-stage cost-aware model routing and measured a real 55 percent cost cut. This is the honest version, including a measurement that looked like a success and was actually a bug.
---

The most dangerous way for an autonomous coder to fail is quietly. A wrong patch
gets caught by tests. A patch that never gets written looks, from a distance, a
lot like a build that is still thinking. This cycle we put a number on that
distance and then closed it.

{/* truncate */}

## The number that started it

The Factory hub ran our first external benchmark: 50 real SWE-bench Verified
tasks, driven through the live pipeline and scored by the official harness, not
by us. AIFactory resolved 38 percent overall. But the informative slice was the
failure breakdown. When AIFactory produced a scorable patch, it was correct about
79 percent of the time. The problem was that 22 of 50 runs produced no patch at
all. Nearly half of our failures were empty. Silent.

You cannot fix what you cannot see, so first we made it visible, and then we
went and found the cause.

## Three defenses against the empty patch

Two independent investigations converged on the same seam: the coding session
could stall on startup. Entering the agent client spawns the local CLI and its
MCP tool servers, two of which are fetched over the network at connect time. On a
cold or network-restricted runner that fetch could hang, no first model call ever
happened, and with no timeout anywhere on the path the session burned in silence
straight to the Kubernetes job deadline. Zero tokens, zero output, one empty
patch.

We fixed it in depth rather than in one place:

- A connect and first-token watchdog. If entering the session does not complete
  within a bounded window, it now fails fast into the existing retry path instead
  of hanging for an hour. A silent stall becomes a loud, recoverable error.
- Pre-baking the MCP tool servers into the runner image, so the network fetch
  that caused the stall does not happen at run time at all.
- Bounding the rate-limit wait below the build deadline, so no single wait can
  consume the whole build.

While we were there we found a second, quieter no-op: a plan whose subtasks
omitted a status field would select nothing, and the coder would exit reporting
that there was nothing to do. That was a one-line divergence between two loaders,
and it was writing zero code on real plans. Fixed, with a regression test.

## Then we made it cheaper, and caught ourselves doing it wrong

The same benchmark gave our cost-aware routing work something to prove against.
The idea of RFC-0014 is simple: not every stage needs the most expensive model.
Planning benefits from a frontier model; bulk coding often does not. So we route
per stage.

The honest part is what happened when we first measured it. We ran the same tasks
with routing off and on, and got identical cost. That looked like the feature did
nothing. It turned out routing genuinely did nothing, because the difficulty tier
was writing a fixed model into the task metadata that silently outranked the
router. That is a real bug, and the measurement is what exposed it. We fixed the
precedence so routing is consulted before the static tier model, added a
capability floor so a hard task can never be routed below the model it needs, and
re-measured.

The second measurement was clean. On three identical tasks, routing on took
planning to a frontier model and coding to a cheap one, and cost dropped from
6.48 to 2.91 US dollars, a 55 percent reduction, at essentially the same token
volume. The saving is the model mix, not less work, and every worker in the
completion event is now stamped with the tier it ran at, so you can see it in the
cockpit rather than take it on faith.

## What this proves

That we treat reliability as a measurable property, not a vibe. The empty-patch
rate was the dominant gap in a public benchmark, we found the mechanism, and we
defended against it in the image, in the session, and in the retry policy.

That we would rather ship a fix we can defend than a number we cannot. The
routing story only counts because we were willing to notice that our first result
was wrong and say so.

And that honesty scales: a benchmark that looked like a bad day was the best
thing that happened to the coder all cycle.

## What is next

Turn the routing measurement into a standing matrix instead of a one-off, and get
the scored-resolve confirmation now that the benchmark patches are clean of
scaffolding noise. Bring the second coder runtime, Codex, to the same bar as
Claude; this cycle it learned to report its own failures out loud instead of
exiting quietly, which is the prerequisite for trusting it at all. The gaps we
have not closed are written down in public issues, which is the only kind of
roadmap worth having.
