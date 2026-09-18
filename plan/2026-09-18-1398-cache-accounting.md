---
status: approved
issue: 1398
spec: spec/2026-09-18-1398-cache-accounting.md
---

# Plan: Record cache creation and cache reads separately

## Decisions (carried from the approved intent and spec)

- `system_instructions` **keeps its meaning** (cache creation + reads). Nothing already
  stored or rendered changes value.
- Additive fields only: `cacheReadTokens` and `cacheCreationTokens` in `token_usage.json`
  (top level and per worker), plus `cacheHitRate` = read / (read + creation), or `null` when
  both are 0, in the API shape. `cache_read_tokens` / `cache_creation_tokens` go in the
  RFC-0001 completion `usage` block.
- `version` stays 1. Old files load with the new fields defaulted to 0.
- The 1-hour TTL is out of scope (a later intent, once this data exists).
- **Gate before shipping:** CFactory must accept the two new `usage` keys (spec risk). If its
  schema is strict, step 5 ships behind that check (see step 0).

## Steps

0. **CFactory contract check.** Read the RFC-0001 `usage` schema in the CFactory repo
   (`/mnt/data/Source-home/GitHub/CFactory`, the completion-event ingest model) and record
   whether unknown `usage` keys are accepted or rejected.
   → verify: a one-line finding appended to this plan. If **rejected**, skip step 5 in this
   PR and open a CFactory issue for a coordinated change. Everything else still ships.
1. `apps/backend/agents/token_attribution.py`:
   - `TurnAttribution` (`:185`): add `cache_read_tokens: int = 0` and
     `cache_creation_tokens: int = 0`. `attribute_turn` (`:194`) copies them from `usage`.
     The category math is untouched.
   - `_empty_aggregate` (`:285`): add `"cacheReadTokens": 0` and `"cacheCreationTokens": 0`.
   - `_read_aggregate` (`:319`): `setdefault` both to 0, beside the existing backfills.
   - `record_turn` (`:500-520`): add the turn's two counts to the aggregate.
   - `_fold_worker` (`:361`): new keyword args `cache_read_tokens` and
     `cache_creation_tokens`, default 0, summed into the worker record. The caller in
     `record_turn` passes them.
   - `render_breakdown` (`:586`): emit `cacheReadTokens`, `cacheCreationTokens` and
     `cacheHitRate`.
   → verify by step 2.
2. `tests/test_token_attribution.py`: new tests:
   - read=900 / creation=100 → the aggregate stores 900 / 100, `system_instructions` is
     still 1000, and categories still sum to `totalInputTokens`;
   - two turns accumulate, and the per-worker records carry the split;
   - a legacy file without the keys loads → 0 / 0, with `cacheHitRate` `null`;
   - a provider that reports no cache → 0 / 0, `null` rate, and the no-cache category
     path is unchanged.
   → verify: `apps/backend/.venv/bin/pytest tests/test_token_attribution.py -v` passes.
     Mutation: dropping the `record_turn` sum fails the accumulation test.
3. `apps/frontend-web/src/components/task-detail/TokenUsagePanel.tsx`: add
   `cacheReadTokens?`, `cacheCreationTokens?` and `cacheHitRate?: number | null` to
   `TokenUsageData` (`:28`), all optional because old responses lack them. Render one line,
   "Cache: N read · M written · X% hit", only when either count is greater than 0.
   New i18n keys `tasks:tokenUsage.cacheRead`, `.cacheWritten` and `.cacheHitRate` go in
   **`en`, `fr` and `pt-BR`** (`src/shared/i18n/locales/*/tasks.json`). No hardcoded strings.
   → verify: `npm run typecheck`, `npm run lint` and `npm test` (vitest) pass in
   `apps/frontend-web`.
4. The web-server read path needs no change: `routes/tasks_usage.py` returns
   `read_breakdown`, which now includes the keys.
   → verify: an existing tasks-usage route test, or a new one, asserts the keys are present.
5. `apps/web-server/server/services/completion.py` `usage_from_aggregate`: add
   `"cache_read_tokens"` and `"cache_creation_tokens"` from the aggregate, with existing
   keys unchanged. **Skipped if step 0 found a strict schema.**
   → verify: `apps/web-server/tests/test_completion_emitter.py` gets a case asserting both
   keys, and a golden check that the pre-existing keys are unchanged.
6. Lint gates before committing: `ruff format --check`, plus the cq-ratchet (ruff and mypy)
   after committing (`--base origin/dev`, 0 regressed).

## Tests

```bash
apps/backend/.venv/bin/pytest tests/test_token_attribution.py -v
(cd apps/web-server && ../backend/.venv/bin/pytest tests/test_completion_emitter.py -q -o asyncio_mode=auto)
(cd apps/frontend-web && npm run typecheck && npm run lint && npm test)
```
Expected: all pass. Existing token-usage tests pass unchanged.

## Rollback

Revert the commit. Files written with the new keys stay readable by the old code, which
ignores unknown keys. No migration.
