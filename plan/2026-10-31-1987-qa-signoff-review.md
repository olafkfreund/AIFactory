---
status: draft
issue: 1987
spec: spec/2026-10-31-1987-qa-signoff-review.md
coders:
  1: codex   # wave lane: codex is the named coder
  2: codex
  3: codex
---

# Plan: a coder-written QA pre-approval must not skip QA when review is required

Approved decision (owner, option c): when the run-start snapshot
`RunGates.requires_review` is true, a `qa_signoff.status=approved` present
before QA runs is ignored and the QA loop always runs. Read nothing new from
the worktree; use `gates.requires_review` only. Behaviour without required
review is unchanged. `cli/qa_commands.py` (`--qa`) is not changed.

## Steps

1. `apps/backend/qa/criteria.py` ~53 and ~92: add keyword `requires_review: bool = False`
   to `is_qa_approved` (return False when true, with a one-line `#1987` comment) and to
   `should_run_qa` (pass it to `is_qa_approved`). Update docstrings in one line.
   `apps/backend/cli/build_commands.py` ~615 and ~663: call both with
   `requires_review=gates.requires_review`.
   → verify by `.venv/bin/pytest tests/test_qa_criteria.py tests/test_run_gates_snapshot.py -q`
   Traps: `gates` is captured at ~529 before the coder runs; do not call
   `RunGates.capture` again. Existing tests patch these with MagicMock, so a new kwarg is safe.
2. `apps/backend/qa/loop.py` ~222: keep the early `return True` only when
   `not gates.requires_review`. When review is required and the plan carries an
   approved `qa_signoff` (and there is no human feedback), remove `qa_signoff` from
   the plan with `load_implementation_plan` / `save_implementation_plan` (both
   already in `qa/criteria.py` / `agents.utils`) before the first reviewer session,
   so `qa/reviewer.py` ~322 reads the reviewer's own verdict.
   → verify by `.venv/bin/pytest tests/test_run_gates_snapshot.py tests/test_qa_loop*.py -q`
   Traps: `gates` is already resolved at the top of the function; the loop's
   later status reads must stay as they are.
3. New `tests/test_qa_signoff_review_gate.py` (reuse the patch harness of
   `tests/test_run_gates_snapshot.py::test_build_gates_survive_files_forged_during_the_coder_session`
   and `::test_qa_loop_ignores_metadata_forged_by_a_qa_session`):
   - build path, plan with all subtasks `completed` and `qa_signoff.status=approved`,
     real `should_run_qa`: `requireReviewBeforeCoding=True` calls the patched
     `run_qa_validation_loop`; without it, it is not called.
   - loop with gates `requires_review=True` and a pre-approval: reaches the reviewer
     session and the plan has no `qa_signoff` at that moment; with `False` it
     returns True without a session.
   → verify by the new file passing, and failing with steps 1-2 reverted.
   Traps: do not leave module mocks in `sys.modules` (#1988).

## Tests

`cd apps/backend && .venv/bin/pytest ../../tests/test_qa_signoff_review_gate.py ../../tests/test_qa_criteria.py ../../tests/test_run_gates_snapshot.py -q`,
`ruff check` / `ruff format --check` on touched files, `scripts/ratchet_lint.py` if present. All pass.

## Rollback

Revert the PR commit; no data or config migration.
