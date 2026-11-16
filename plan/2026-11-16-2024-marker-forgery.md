---
status: draft
issue: 2024
spec: spec/2026-11-16-2024-marker-forgery.md
coders:
  1: lead   # three-line edit in one function, below the handoff threshold
  2: lead   # tests extend the existing marker harness
reviewers:
  - codex   # independent read of the diff
  - agy     # second model, different blind spots
---

# Plan: Job log markers cannot set terminal state

Branch `fix/2024-marker-nonce-forgery` from `origin/dev`.

Approved decisions (from the spec): a phase marker may set only
`spec_creation`, `planning`, `coding`, `qa_review`, `qa_fixing`. Markers
naming `complete`, `completed` or `failed` are telemetry: logged, never
tracked, no progress/status event, one warning. Terminal state stays with
exit/Job reconciliation (a forged non-terminal phase can still shift the
exit-time COMPLETED vs PLAN_REVIEW label; accepted, see spec). Nonce stays in the runner env.

## Steps

1. `apps/web-server/server/services/agent_emit.py` `_handle_output_line`
   (~:594): drop the three terminal entries from `phase_map`; when the marker's
   phase is one of `complete`/`completed`/`failed`, log a warning and return
   `current_phase` before any state or progress change. Remove the now-dead
   COMPLETED/FAILED branches in the log-writer block.
   → verify: `cd apps/web-server && .venv/bin/pytest ../../tests/test_marker_nonce.py -q`
   Traps: AIFactory ruff (plain imports); keep the line still logged (it is
   logged earlier in the function).
2. `tests/test_marker_nonce.py`: add valid-nonce `complete` and `failed`
   cases through the in-process handler and the kubejob stream; assert the
   phase is unchanged and `_emit_progress` is not awaited.
   Cover the `completed` alias, the retained log line and no status emission.
   → verify: same command; the new tests fail without step 1.
   Traps: none.

## Tests

`pytest tests/test_marker_nonce.py tests/test_phase_event.py tests/test_kubejob_execution_progress.py tests/test_spec_job_backend.py -q`; `ruff check` and `ruff format --check` on touched files.

## Rollback

Revert the commit; markers return to setting any phase.
