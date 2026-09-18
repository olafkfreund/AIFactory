---
status: approved
issue: 1398
intent: intent/2026-09-18-1398-cache-accounting.md
---

# Spec: Record cache creation and cache reads separately

## Where the split is lost

The SDK reports both counts per turn, and they reach the accounting:
`agents/coder.py:784-788` and `parallel_integration.py:573-577` pass
`cache_read_tokens` / `cache_creation_tokens` into `TurnUsage`
(`token_attribution.py:136-143`), and `session.py:676-708` even logs them per turn. They
are dropped in one place: `attribute_turn` (`:217-223`) sums them into the
`system_instructions` category, `TurnAttribution` (`:185`) has no cache fields, and
`record_turn` (`:500-520`) folds only categories into `token_usage.json`. The aggregate
(`_empty_aggregate`, `:285`) and the API shape (`render_breakdown`, `:586`) therefore
cannot tell them apart.

## Design

Following the approved decisions (`system_instructions` stays the sum; two fields are added):

1. **`TurnAttribution`** gains `cache_read_tokens: int = 0` and
   `cache_creation_tokens: int = 0`, copied from `usage` in `attribute_turn`. The category
   math is unchanged, so every existing total reconciles exactly as before.
2. **Aggregate** (`token_usage.json`): two new top-level integers, `cacheReadTokens` and
   `cacheCreationTokens`, summed in `record_turn`. `_read_aggregate` defaults them to 0, so
   old files load unchanged. `version` stays 1: the change is additive, and readers already
   ignore unknown keys. The per-worker records (`_fold_worker`) get the same two fields, so
   a parallel build's split is visible per worker.
3. **API/UI shape:** `render_breakdown` exposes `cacheReadTokens`, `cacheCreationTokens`
   and a derived `cacheHitRate` = read / (read + creation), or `null` when both are 0.
   `TokenUsagePanel.tsx` shows the two figures and the hit rate, using i18n keys in
   `en`/`fr`/`pt-BR`.
4. **Completion event** (`services/completion.usage_from_aggregate`): the RFC-0001 `usage`
   block gains `cache_read_tokens` and `cache_creation_tokens`, additive, with the existing
   keys unchanged.

## Alternatives rejected

- **Two new categories in place of `system_instructions`.** This redefines a field that the
  UI, CFactory and existing files already read. The approved decision is to keep it.
- **Store per-turn raw usage.** The file grows with every turn, and the question the intent
  asks (how often the cache is re-created) is answered by the two totals plus the per-worker
  split.
- **Derive the split later from logs.** `session.py` logs it, but logs are not durable and
  not per task.

## Risks

- **CFactory's RFC-0001 `usage` schema.** If it validates strictly, the two new keys could
  be rejected. They must be checked against the CFactory contract before this ships. If it
  is strict, the keys go to CFactory in a coordinated change and AIFactory writes them only
  to the file and API first.
- The UI panel is the only frontend change. The i18n keys must exist in every locale, or the
  build's i18n check fails.
- Providers that report no cache (local OpenAI-compatible endpoints) record 0/0, and the hit
  rate is `null`, not 0%.

## Verification

- Unit (`token_attribution`): a turn with read=900 and creation=100 stores
  `cacheReadTokens` 900 and `cacheCreationTokens` 100. `system_instructions` is still 1000.
  Category totals still sum to `totalInputTokens`.
- An old `token_usage.json` without the keys loads and renders 0/0 with a `null` hit rate.
- `render_breakdown` and `usage_from_aggregate` carry the new keys. Existing keys are
  byte-identical in a golden comparison.
- Frontend: the panel renders both figures. `npm run lint` / typecheck and the i18n check pass.
