---
status: approved
issue: 389
spec: spec/2026-10-24-389-memory-dup-baseline.md
---

# Plan: memory duplicate-rate baseline

Decisions carried from the approved spec: lexical bag-of-words cosine by
default, `--embedder` opt-in with lexical fallback, exact pairs by folded
text, near pairs cosine >= 0.88 and not exact, read-only, JSON output.

## Steps

1. scripts/memory_dup_baseline.py: load items, count_pairs, main → verify by
   `python scripts/memory_dup_baseline.py <dir>`
   Traps: scripts/ is ruff-gated (ruff.toml); stdlib only; never write to the store.
2. tests/test_memory_dup_baseline.py: tiny fixture for count_pairs and load_items
   → verify by `pytest tests/test_memory_dup_baseline.py`
   Traps: tests must be collected (uncollected-tests-allowlist.toml).
3. Run read-only in the aifactory pod via kubectl exec (script copied to /tmp
   in the pod) and write docs/dev/memory-baseline-2026-10.md → verify by
   reading the doc.
   Traps: no secrets, IPs or names in the doc.

## Tests

`pytest tests/test_memory_dup_baseline.py`, `ruff check scripts tests`.

## Rollback

Revert the PR; nothing else depends on the script.
