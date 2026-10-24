# Memory duplicate-rate baseline (RFC-0021, #389)

Date: 2026-10-24. Project: `aifactory-demo` (live aifactory pod). Specs: 1.
Pooled items: 33 (gotchas plus session-insight strings). Store was read only.

| Measure | Count |
| --- | --- |
| Exact duplicate pairs (case and whitespace folded) | 19 |
| Near-duplicate pairs (cosine >= 0.88, not exact) | 80 |

**Measure used: lexical, not embedder.** The pod has no embedder configured
(no `GRAPHITI_*` or `OLLAMA_EMBEDDING_MODEL`), and an embedder call costs
quota, so similarity is a bag-of-words term-count cosine at the same 0.88
threshold. It under-counts paraphrases, so these are lower bounds for true
semantic duplication. Re-run with `--embedder` once one is configured.

**Caveat on the numbers.** 15 of the 33 items are the auto-recorded
`Implemented subtask: <id>` boilerplate, which differs only by id and drives
most of the near pairs. The signal for RFC-0021 Phase 1 is that pooled memory
is mostly repetition: the promotion gate should filter this class first.

Other projects, same method: `pfactory-friends-demo` 14 items, 11 exact, 4 near
(5 specs); `AIFactory` 6 items, 3 exact, 0 near (1 spec).

Reproduce: `python3 - <project_dir> < scripts/memory_dup_baseline.py`
(inside the pod, via `kubectl exec -i`; nothing is written).
