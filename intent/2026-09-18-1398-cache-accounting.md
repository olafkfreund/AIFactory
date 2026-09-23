---
status: approved
issue: 1398
author: Olaf Krasicki-Freund
---

# Intent: Token accounting cannot tell a cache write from a cache read

## Problem

#1398 was filed as "99.4% of a task's tokens are system-prompt overhead". The issue's own
follow-up comments retracted that premise. `agents/token_attribution.attribute_turn`
(`:217-223`) sets `system_instructions = cache_read_tokens + cache_creation_tokens`, so
that category is **the cached token count**, not a measure of prompt size.

What is actually wrong is narrower, and it hides a real cost question:

1. **Cache writes and cache reads are summed into one number.** They differ about 12.5× in
   price (a write costs more than an uncached token; a read costs a fraction). A build
   that re-creates its cache every turn and one that reads it every turn look identical
   in `token_usage.json` and on the dashboard. That is why nobody noticed which one is
   happening.
2. **The build path cannot choose its cache TTL.** SDK sessions get their system prompt
   as a plain string (`core/client.py:970`, `core/cache.build_cached_system_str`), because
   `ClaudeAgentOptions.system_prompt` only accepts `str` (`core/cache.py:130-140`). There
   is no `cache_control` marker, so the 5-minute default applies. Any gap over 5 minutes
   between turns (a slow tool call, a QA iteration, a retry) re-creates the cache at write
   price. `build_cached_system_blocks(..., ttl="1h")` exists but only serves direct-API callers.

## Proposed outcome

Every recorded turn reports cache **creation** and cache **reads** separately, all the way
to `token_usage.json`, CFactory's rollups and the UI. From real builds you can then see
how often the cache is re-created, and so whether the 1-hour TTL (item 2) is worth pursuing.

## Affected users and systems

- `apps/backend/agents/token_attribution.py` and whatever serialises its categories
  (`token_usage.json`)
- Consumers of those fields: the completion event's usage (CFactory rollups), the web UI's
  usage views
- Operators judging build cost

## Constraints

- Backward compatible. Existing readers of `system_instructions` keep getting a value
  (sum or alias) until they migrate. Old `token_usage.json` files must still load.
- Accounting only. This task does not change how prompts are built or cached.
- No new provider calls to measure anything; the SDK already reports both counts per turn.

## Open questions

1. Should `system_instructions` keep its current meaning (the sum, for compatibility, with
   two new fields added), or be redefined and the consumers updated in the same change?
2. Is the 1-hour TTL (problem 2) in scope for a **later** intent once the split data exists?
   My recommendation is yes, because it needs a route around the SDK's str-only
   `system_prompt` and is an architecture decision, not a cleanup. I'd also retitle #1398
   to "Split cache creation from cache reads in token accounting".

## Decisions (at approval, 2026-09-18)

1. `system_instructions` **keeps its current meaning** (creation + reads, for compatibility).
   Two new fields, `cache_creation` and `cache_read`, are added beside it.
2. The 1-hour TTL is **out of scope**. It gets its own intent once the split data shows how
   often the cache is re-created. #1398 is retitled to match.
