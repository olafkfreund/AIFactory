---
status: draft
issue: 2022
spec: spec/2026-11-16-2022-session-file-collide.md
coders:
  1: lead   # ~15 lines per repo; a handoff costs more than it saves
  2: lead
reviewers:
  - codex
  - agy
---

# Plan: session file collisions and gate edge cases (#2022, #2035)

Approved decisions (from the spec): never overwrite a session file (bump the
number to the next free one); `repeat_key` total on mixed-type keys; escape
header lines in gotcha bodies; same fix in PFactory and TFactory; the
Graphiti-primary path stays ungated (documented limit).

## Steps

1. AIFactory `apps/backend/memory/sessions.py` (~L60-80) and
   `memory/novelty.py` (~L14): exclusive-create ("x") retry loop, drop the self-skip; persisted-form
   key, None on TypeError/ValueError. ->
   `pytest tests/test_memory_novelty.py`.
   Traps: AIFactory ruff sorting; none for logic.
2. AIFactory `apps/backend/agents/tools_pkg/tools/memory.py` (~L130-150):
   injective backslash escape of header lines before dedupe and write. -> same test file with new cases.
3. Add tests to `tests/test_memory_novelty.py`. Lead.
4. Port steps 1-3 to PFactory and TFactory worktrees (`git diff` apply or
   same edits; keep their formatting). Lead -> their memory tests.

## Tests

`cd apps/backend && .venv/bin/pytest ../../tests/test_memory_novelty.py` (or
`just test` form), `ruff check` and `ruff format --check` on touched files.

## Rollback

Revert the PRs; session files already written stay valid.
