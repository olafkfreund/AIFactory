---
slug: proof-over-promises
title: "Proof over promises: putting AIFactory on a benchmark"
authors: [olaf]
tags: [ai-coding, devops, multi-provider, self-hosted]
date: 2026-06-12
description: We stopped and reviewed AIFactory against its own goals and against Devin, Factory.ai and the cloud agents. The verdict — the pipeline works, but we'd published zero numbers. So we started measuring. Here's the honest progress report, including the four bugs the first run found in our own harness.
---

This week I did something uncomfortable: I stopped shipping features and reviewed
AIFactory honestly — against its own goals, and against the 2026 field of
autonomous coding tools. Not the demo-day version. The version you'd give an
investor who's going to check.

The short verdict: the pipeline works, end to end, and on the axis that actually
matters in 2026 — governance and verification — it's ahead of the pack. But we
had **published zero numbers**. We built the part of the problem that doesn't
commoditize and then never measured it. This post is what we found and what
we're doing about it.

{/* truncate */}

## Where AIFactory actually stands

AIFactory is the build leg of a four-service suite: plan, build, verify, observe.
The build leg itself is in good shape and I'll claim it plainly:

- **The planner → coder → QA loop is production-grade.** Eight providers behind
  one abstraction (Claude, Codex, Gemini, Ollama and more), parallel build waves
  in isolated git worktrees, act-loop guardrails that stop an agent looping on a
  failing edit, and a mutation ledger that checkpoints before every change. The
  backend carries 313 test files and a near-zero TODO density in the core paths.
- **The loop is closed.** A signed plan comes in, code goes out, the change gets
  deployed to real cloud infrastructure, the tests run against the *live*
  endpoint, and a reviewed PR can auto-merge on a green verification. That's not
  a roadmap sentence — it ran on real AWS App Runner, with teardown, last week.

So what's wrong? Nothing in the capability column. The gap is **evidence**.

## The market moved toward us

The 2026 landscape made the bet clearer, not muddier. Code generation is
commoditizing fast — the benchmark wars between Droid, Devin, OpenHands and the
lab CLIs are a race we deliberately aren't running. Meanwhile the industry's own
surveys say the quiet part out loud: most organizations can *monitor* their
agents but can't *stop* them, only a minority of developers trust agent output,
and the EU AI Act's high-risk obligations — logging, human oversight, audit
trails — land on **2 August 2026**.

The bottleneck everyone now names is verification, trust, and auditability. That
is precisely what AIFactory and its sibling services were built for. On that
axis nobody in the comparison set has the full set we do: a pre-code governance
gate, an independent verification leg that tests the deployed service, real
deploy-then-verify, an HMAC-anchored audit chain, and a cockpit that can pause
the fleet. And because we *wrap* coding agents instead of competing with them,
someone else topping a coding benchmark isn't a threat — it's a candidate
provider.

## Where we're honestly behind

Three places, and none of them are unwritten code:

1. **Proof.** Competitors market benchmark numbers relentlessly. We built a full
   PARR benchmark harness — four scenarios across multiple providers — and had
   never run it. Our strongest evidence was sitting on a shelf.
2. **Customers.** Zero design partners yet; the pricing page is illustrative.
3. **One security item.** Agents still run with an effectively bypassable
   command allowlist rather than a real OS sandbox. Ten of eleven findings from
   our own security audit are closed; this is the eleventh, and you cannot sell
   "trust by construction" while it's open.

All of this is now tracked in one program epic — *Make it great: prove, harden,
and sell the PARR spine* — ordered deliberately: prove first (it's days of work
and the highest leverage), then close the one trust blocker, then adversarially
validate the loops we just shipped, then sell with the artifacts the first three
phases produce.

## First contact: the benchmark found four bugs — in our own harness

Here's the part I like, because it's the kind of honesty this project trades on.

The very first time I pointed the benchmark at our **deployed** fleet, it failed
in seven seconds — and every failure was a cross-service seam bug, the exact
class of problem that unit tests per repo never catch:

- **Cloudflare blocked the bot.** The harness's default Python user-agent got a
  `403` from the managed challenge in front of the live services. Fix: send a
  real browser user-agent.
- **A transient `500`.** One factory cold-started mid-request. Fix: retry `5xx`
  with backoff instead of failing the run.
- **A `409` on re-run.** The deployed factories register repos under derived
  names, so blindly creating the project conflicted every second run. Fix: reuse
  the existing project by URL or name.
- **Endpoint drift.** The verify leg moved to a new ingest contract in a recent
  release and the harness still spoke the old one. Fix: speak the current API and
  let the planner auto-run on ingest.

None of those were caught locally because locally there's no Cloudflare, no cold
start, no months-old registered project, and no version skew between services.
The lesson is blunt: our unit-test posture is excellent and our risk profile is
*cross-service*. So alongside the benchmark, we're adding a nightly end-to-end
run that exercises every seam and fails loudly — so boundary bugs get caught by
a gate instead of by a user.

The harness is fixed, the run is back in flight against the live fleet, and the
numbers — time-to-verified-PR, token cost, handback cycles, pass rate, per
provider — go into a public results table as they land. The point was never a
vanity score. It's to be able to answer the only question that matters to a
regulated team: *can you prove it?*

We spent a year building the part of autonomous coding that doesn't commoditize.
The review says it works. Now we prove it in the open — numbers, warts, and all.

AIFactory is open source and self-hostable. If governed, auditable autonomous
coding with no vendor lock-in is the problem you have too, the code's on
[GitHub](https://github.com/olafkfreund/AIFactory).
