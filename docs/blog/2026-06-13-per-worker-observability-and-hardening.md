---
slug: per-worker-observability-and-hardening
title: "Per-worker observability: who spent the tokens, and the security pass that came with it"
authors: [olaf]
tags: [ai-coding, devops, multi-provider, observability]
date: 2026-06-13
description: AIFactory already ran independent subtasks in parallel across multiple providers. The gap was that the cost and timing came back as one aggregate number — you couldn't see which worker, provider or model spent what. This session closes that gap with per-worker token attribution, OpenTelemetry metrics, live per-worker events, and an observe-only budget alert — plus an Actions security pass and a god-file decomposition that landed alongside it.
---

A few weeks ago we taught the build executor to run independent subtasks in
parallel, across multiple LLM providers at once. That work was about throughput,
and it worked. But it left a blind spot: when a build finished, the cost and the timing came back as a single
aggregate. Total tokens. Total dollars. Total wall-clock. Useful, but it couldn't
answer the question you actually ask after a parallel run: *which* worker spent
that money, on *which* provider, in *which* model, and where did the time go?

This session was about closing that gap. The headline is per-worker
observability. The theme is that all of it is **additive** — the parallel build
spine didn't change, we just put instruments on it. The same session also carried
a GitHub Actions security pass and a god-file decomposition, because they were
sitting in the same backlog and they unblock the work above.

{/* truncate */}

## The shape of the problem

The executor already fans subtasks out to a pool of workers, each potentially on a
different provider/model. What it reported back was a scalar: one `usage` object
summing everything. That's the right number for a budget line, but it's the wrong
granularity for an operator. If a build cost more than you expected, "the build
cost $4.10" doesn't tell you that one worker on an expensive model did 70% of the
spend while three cheap workers idled. You need the per-worker breakdown to make
that call.

So the work was a chain of small, individually-shippable changes, each behind the
required backend CI gate, each preserving the existing aggregate so nothing
downstream broke.

## Per-worker token attribution (#566)

The first change records per-worker token, cost and duration in
`token_usage.json`. The file gains a `workers` map — one entry per worker, keyed
so you can see input/output tokens, USD cost and duration for each — while the
**scalar aggregate is preserved exactly as it was**. Anything that read the old
top-level number keeps working; anything new can read the map.

On the wire, the build completion event grows to **v1.3** with an additive
`usage` block:

- `usage.workers[]` — the per-worker rows.
- `usage.by_provider` — the same spend rolled up per provider.
- `usage.by_model` — and per model.

"Additive" is the important word. Older consumers see a completion event that
still has everything they expect; newer consumers get the breakdown. No field was
removed or repurposed.

## OpenTelemetry metrics, emitted from the web-server (#567)

Attribution in a JSON file is good for the portal panels. For an operator running
Grafana or Datadog, you want metrics. So #567 emits four OpenTelemetry
instruments:

- `gen_ai.input_tokens`
- `gen_ai.output_tokens`
- `gen_ai.cost_usd`
- `worker.duration_ms`

Each is tagged `{provider, model, phase}`. Two deliberate decisions here are worth
calling out, because they're the kind of thing that bites you later if you get
them wrong.

**The metrics are emitted from the web-server, not the agent.** AIFactory's agent
subprocesses deliberately don't export telemetry — they inherit the parent trace
*context* (so the trace ID is consistent) but they don't run their own exporter.
That's a long-standing design choice: the agent stays a clean subprocess, and the
web-server, which already owns the OTel SDK lifecycle, is the single place that
talks to the collector. Per-worker metrics fit that model — the web-server already
receives the completion data, so it's the natural emission point.

**Cardinality is bounded.** The tag set is `{provider, model, phase}` and nothing
else — in particular, **never `task_id`**. Per-task labels are exactly how you blow
up a time-series database: unbounded label cardinality turns a metric into a
log. Provider, model and phase are small, closed sets, so these series stay cheap
no matter how many builds you run.

And, consistent with the rest of the tracing stack, the whole thing is a **no-op
when OTel is disabled** — instruments build in memory and drop at export when
there's no collector configured. This change also fixed the completion-event
`traceparent` so it links to the real active span rather than a detached context,
so the metrics and the trace line up in your backend.

## Live per-worker events (#568, #570)

Attribution after the fact is half the story. While a build is running you want to
watch workers finish. #568 emits a live `phase: "worker"` sub-event as each worker
completes — so the portal (and any event consumer) sees workers retiring in real
time instead of waiting for the final roll-up.

#570 adds the other half: throttled `phase: "worker_progress"` heartbeats, emitted
roughly every 10 seconds, so a long-running worker visibly ticks rather than
sitting silent between start and finish. The throttle keeps the event stream from
turning into a firehose on a big fan-out.

## An observe-only budget alert (#569)

The last observability piece is a soft budget alert. The completion `usage` block
gains a `budget` sub-object — `{limit, spent, exceeded}` — and a `budget.exceeded`
OTel counter fires when spend crosses the configured limit.

The critical design decision: **this is observe-only and never aborts a build.**
It is an alert, not a circuit breaker. A build that runs over budget still finishes
and still hands off; you just get told. Hard-stopping a half-finished build on a
budget threshold would trade a known, bounded cost overrun for an unknown amount of
wasted work and a broken pipeline — a bad trade. If a hard cap is ever wanted,
it'll be a separate, explicit opt-in, not a side effect of the alert.

## The security pass that came with it (#557, for issues #553 / #554)

Two CI hardening changes landed in the same session.

The first fixed a **GitHub Actions script-injection**. An untrusted issue title was
being interpolated directly into a shell step — the classic Actions injection
vector. The fix moves untrusted values into `env:` bindings so they're passed as
data, never expanded as shell.

The second hardened `copilot-pr-review.yml`. It had been trusting any actor whose
name ended in `[bot]` — the **CVE-2025-66032 pattern**, where a `[bot]` suffix is
trivially spoofable and grants a workflow elevated trust it shouldn't. That suffix
check was replaced with a proper `author_association` membership gate, and we
removed `--allow-all-tools` from a path that runs over untrusted PR content — you
don't hand an unrestricted tool surface to something an outsider can influence.

Alongside the security fixes, CI gained a frontend `vitest` job and a `mypy` job,
with broadened triggers. Both start **non-blocking** — they run and report, but
they don't gate merges yet. That's intentional: turn the lights on, see the real
failure rate, then promote to required once the signal is clean. Flipping a brand
new check straight to "required" is how you wedge the whole team behind a flaky
gate on day one.

## God-file decomposition (#556)

Finally, the unglamorous but load-bearing work: `routes/tasks.py` had grown to
**5,346 lines**. This session pulled it down to roughly 4,000 by extracting
cohesive sub-routers — worktree tools (#559), plan-approval (#560), PR-creation
(#563), and inbox (#565).

Two constraints made this safe to ship without a held breath: every extracted
route keeps a **backward-compatible re-export** from the old module, and the routes
themselves are **byte-identical** — same paths, same handlers, just living in a
file that's the right size to read. No behavior change, no route moved, nothing
for a client to notice. It just makes the next person who has to touch task routing
able to find the thing they're looking for.

## Why it all fits together

The connective tissue across these changes is restraint. The parallel build
already existed and was correct — so the observability work is purely additive and
spine-preserving, never a rewrite. Metrics come from the web-server because that's
where the telemetry lifecycle already lives and the agent is deliberately kept
export-free. Cardinality is bounded on purpose. The budget watches but doesn't
intervene. The new CI checks report before they block. The god-file split moves
zero behavior. And every one of these merged behind the same required backend gate
(`ruff` + `pytest`), so the safety net was up the whole time.

The result: after a parallel multi-provider build, you can now answer "who spent
the tokens" — per worker, per provider, per model — in the portal, in your metrics
backend, and live as it happens.
