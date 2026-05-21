# Anthropic Message Batches in AIFactory

> Issue #11 — part of Epic #6 (Claude Code / Agent SDK compliance audit).

AIFactory ships a helper module ([`apps/backend/core/batch.py`](../apps/backend/core/batch.py)) for submitting parallel one-shot completions to Anthropic's [Message Batches API](https://docs.anthropic.com/en/api/messages-batches). Batch-tier requests are billed at **0.5× base rate** on both input and output tokens.

## Status (May 2026)

The helper is shipped as a primitive — it works, is fully tested, and is ready for callers. **There is currently no production caller in the AIFactory backend.**

The first opt-in consumer is an end-of-build "insight sweep" entry point at [`analysis.insight_extractor.extract_session_insights_bulk`](../apps/backend/analysis/insight_extractor.py). It takes a list of completed-subtask records and batches their insight extractions into one API call. No code currently wires it up — it ships as scaffolding for a future deferred-extraction worker.

Filed for follow-up:

- Wire `extract_session_insights_bulk` into an end-of-build sweep that defers per-subtask insight extraction off the critical path.
- Add batching to the BMad per-story spawner (`apps/backend/integrations/bmad/session_spawner.py`) once that path has active callers AND its prompts are rewritten to be tool-free.

## Why not the Claude Agent SDK?

The `claude-agent-sdk` (the Python package AIFactory uses for stateful sessions with tool loops) does **not** expose Batch API helpers — it's session-oriented. The batch helper drops to the raw `anthropic` package (`>=0.84.0`, pinned in `apps/backend/requirements.txt`) for the batch endpoint.

That distinction matters for two reasons:

1. **The raw client needs `ANTHROPIC_API_KEY`** in the environment — separate from `CLAUDE_CODE_OAUTH_TOKEN` which the SDK uses. If `ANTHROPIC_API_KEY` is missing, callers fall back to the sequential SDK path automatically.
2. **Batch API processes one-shot completions only — no tool loops.** Prompts that ask the model to use `Read`, `Glob`, etc. produce stub "I need to read X..." responses. The helper is only safe to use with self-contained text prompts (the insight extractor's analyzer prompt is one — it embeds diff + commit messages inline).

## When the batch path engages

The bulk entry point engages the batch path when **all** of the following hold:

- `len(completions) >= AIFACTORY_BATCH_MIN_JOBS` (default `2`).
- `AIFACTORY_BATCH_DISABLE` is unset (or `0` / empty).
- `ANTHROPIC_API_KEY` is set (or passed explicitly to the caller).

Otherwise it transparently runs the existing per-entry sequential extraction.

## Configuration

| Env var | Default | Purpose |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | _(none)_ | **Required.** Raw API key for the Anthropic SDK. Distinct from `CLAUDE_CODE_OAUTH_TOKEN`. |
| `AIFACTORY_BATCH_MIN_JOBS` | `2` | Below this many concurrent completions, skip the batch path. |
| `AIFACTORY_BATCH_TIMEOUT` | `120` | Hard timeout in seconds for the batch poll loop. On timeout, falls back to sequential. |
| `AIFACTORY_BATCH_DISABLE` | _(unset)_ | Kill-switch — set to `1` to force the sequential path for emergency rollback. |
| `INSIGHT_EXTRACTOR_MODEL` | `claude-3-5-haiku-latest` | Inherited from existing insight extractor — applies to batch entries too. |

## Cost expectations (honest)

The Anthropic Batch API offers a **50% discount on both input and output tokens** for batch-tier requests. The discount appears as `service_tier: "batch"` in the per-result usage object — the raw token counts are unchanged, the discount is applied at billing.

The earlier audit suggested **batch + prompt-caching stacks to ~95% combined savings**. python-pro's verification flagged two corrections to that claim:

1. The discounts stack **multiplicatively**, not arithmetically: a cache-hit input token at batch tier costs `0.10 × 0.50 = 0.05×` of base rate (95% off **on cached tokens only**, not overall).
2. **Cache deduplication across batch entries is currently undocumented.** If Anthropic processes batch entries independently, the 5-minute ephemeral cache TTL could miss on entries 2-N. We have not empirically verified the behavior.

For this reason **Slice 1 ships batch alone** (the verified 50% discount). The helper accepts `system` as either a plain string OR a list of `TextBlockParam` dicts with `cache_control` markers (the shape produced by `core.cache.build_cached_system_blocks`), so cache stacking can be wired in later without API changes.

## Operational notes

- **Batches keep running on Anthropic's servers** even if AIFactory's process exits. The batch will complete (or expire after 24h) regardless. Future enhancement: persist the batch ID to disk so a restarted process can resume polling.
- **`request_counts` is all zeros until `processing_status == "ended"`.** Don't try to render partial progress from intermediate polls — the SDK doesn't expose per-entry status during processing.
- **Result text is at `result.message.content[N].text`**, not `result.text`. The helper iterates the content blocks to find the first `.text`-bearing block (handles tool-use blocks gracefully if a prompt accidentally triggers one).

## Verifying it ran

When the batch path engages, the logs show:

```
INFO core.batch: Batch submitted: id=msgbatch_xyz requests=5
INFO core.batch: Batch ended: id=msgbatch_xyz succeeded=5 errored=0 canceled=0 expired=0
INFO analysis.insight_extractor: Batch msgbatch_xyz ended: succeeded=5 errored=0 service_tiers=['batch'] saving=50%
```

The `service_tiers=['batch']` line confirms the discount was applied.

## Kill-switch

```bash
export AIFACTORY_BATCH_DISABLE=1
```

…forces the sequential path everywhere, no restart required for the next bulk call.

## See also

- [Prompt caching guide](./PROMPT_CACHING.md) (PR #16 — Issue #8)
- [Anthropic Message Batches API docs](https://docs.anthropic.com/en/api/messages-batches)
- [`apps/backend/core/batch.py`](../apps/backend/core/batch.py) — the helper itself
- [`apps/backend/analysis/insight_extractor.py`](../apps/backend/analysis/insight_extractor.py) — `extract_session_insights_bulk`
