---
status: draft
issue: 389
intent: intent/2026-10-24-389-memory-dup-baseline.md
---

# Spec: memory duplicate-rate baseline

## Design

`scripts/memory_dup_baseline.py <project_dir> [--embedder]` reads the pooled
store `<project>/.aifactory/memory/`: bullets and paragraphs of `gotchas.md`,
and every string under `what_worked`, `what_failed`,
`recommendations_for_next_session` and `discoveries` in
`session_insights/session_*.json`. It prints JSON: item count, exact pairs
(identical after case and whitespace folding), near pairs (not exact, cosine >=
0.88), spec count (`.aifactory/specs/`) and the measure used.

Vectors: default is a bag-of-words term-count cosine (lexical, no model call).
`--embedder` uses the existing Graphiti embedder (`create_embedder` with
`GraphitiConfig.from_env()`) and falls back to lexical, saying so, when it is
not configured. The pair-counting function takes vectors, so the unit test
needs no embedder.

## Alternatives rejected

- Embedder by default: the pod has no embedder configured and a model call
  costs quota.
- Dedupe before counting: would hide exactly the duplicates we measure.

## Risks

Lexical cosine under-counts paraphrases, so the baseline is a lower bound; the
doc says so.

## Verification

Unit test with a tiny fixture; a read-only run in the pod against one project.
