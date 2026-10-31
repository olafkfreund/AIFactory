---
status: approved
issue: 1997
spec: spec/2026-10-31-1997-gate-worktree-reads.md
coders:     # the lead named agy for this lane
  1: agy
  2: agy
---

# Plan: Remaining agent-writable reads in the run-start gate snapshot

Approved decisions (from the spec):

- In `RunGates.capture`, `fail_closed = source_spec_dir is None and
  bool(os.environ.get("AIFACTORY_TASK_ID"))`. The web server sets
  `AIFACTORY_TASK_ID` on both dispatch paths (#1972); the CLI doesn't.
  - Fail closed: `solo=False`, `requires_review=True`. The code doesn't read
    `requireReviewBeforeCoding`, `mode` or `soloMode` and logs one warning.
  - `source_spec_dir` None without the env var (CLI `--direct`): today's
    reads plus one warning.
- The approved-plan snapshot is read from `gate_dir`, not `spec_dir`, still
  once inside `capture`.
- Not changed: CLI `--direct`; `get_phase_model_betas` and `get_fast_mode`
  (no production callers); model and thinking metadata on the fail-closed
  path.

## Steps

1. `apps/backend/run_gates.py`, `RunGates.capture` (~lines 52-72):
   - Add `import logging` and `logger = logging.getLogger(__name__)`.
   - Compute `fail_closed` as above.
   - If `fail_closed`: `solo, requires_review = False, True` and log a
     warning saying that review is required because the server-driven run
     has no source spec dir.
   - Otherwise read `solo` and `requires_review` from `gate_dir` as today.
     When `source_spec_dir is None`, log a warning that the gates come from
     the run's own spec dir.
   - Pass `gate_dir` to `_approved_plan_snapshot` instead of `spec_dir`.
   - Update the class docstring or a comment with a one-line why (#1997).

   → verify by `cd apps/backend && .venv/bin/ruff check run_gates.py && .venv/bin/ruff format --check run_gates.py`
   and `python scripts/ratchet_lint.py` if it exists.
   Traps: don't change `capture`'s signature (several callers use it). Keep
   `metadata` read from `gate_dir`. Don't touch `phase_config.py`.

2. `tests/test_run_gates_snapshot.py`:
   - Add `"AIFACTORY_TASK_ID"` to the `_clean_env` fixture's `delenv` list.
   - New tests:
     a. With `AIFACTORY_TASK_ID` set and `source_spec_dir=None`, spec metadata
        `{"requireReviewBeforeCoding": False, "soloMode": True}` gives
        `requires_review is True` and `solo is False`. The same holds when
        the metadata is written after `capture`.
     b. Without the env var, the same metadata gives `solo is True`, and a
        warning is logged (`caplog`).
     c. The worktree and source have different `implementation_plan.json`,
        with `requireReviewBeforeCoding: True` on the source. `approved_plan`
        equals the source plan. Rewriting either plan after `capture` leaves
        `approved_plan` unchanged.

   → verify by `apps/backend/.venv/bin/python -m pytest tests/test_run_gates_snapshot.py -q`
   (all pass). Tests a and c fail without step 1.
   Traps: #1988 is a local-only test-order failure that isn't ours; CI
   decides.

## Tests

- `apps/backend/.venv/bin/python -m pytest tests/test_run_gates_snapshot.py tests/test_routing_policy.py -q` → green.
- `apps/backend/.venv/bin/ruff check apps/backend/run_gates.py tests/test_run_gates_snapshot.py` → clean.

## Rollback

Revert the implementation commit. There is no data or config migration.
