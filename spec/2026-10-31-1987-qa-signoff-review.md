---
status: approved
issue: 1987
intent: intent/2026-10-31-1987-qa-signoff-review.md
---

# Spec: a coder-written QA pre-approval must not skip QA when review is required

## Design

Chosen (owner decision, option c): when `RunGates.requires_review` is true,
ignore a `qa_signoff.status=approved` found before QA runs.

- `qa/criteria.py`: `is_qa_approved(spec_dir, requires_review=False)` returns
  False when `requires_review` is true. `should_run_qa(spec_dir,
  requires_review=False)` passes it through. Default keeps every other caller
  unchanged.
- `cli/build_commands.py` (~615, ~663): pass `requires_review=gates.requires_review`
  (the snapshot captured at ~529). With review required, the build-complete
  path always enters `run_qa_validation_loop`; the "pre-approved by coder"
  branch is never taken.
- `qa/loop.py` (~222): the loop's own "already approved" early return applies
  only when `gates.requires_review` is false. When it is true, the loop drops
  the pre-existing `qa_signoff` from the plan before the first reviewer
  session, so the verdict read after that session (`qa/reviewer.py` ~322) is
  the reviewer's own and not the stale coder approval.

## Alternatives rejected

- (a) Keep today's behaviour (pre-approval skips QA, reported as unverified): owner chose (c).
- (b) Require trailing-gate evidence written by `run.py` outside the agent-writable tree: larger change, owner chose (c).
- Ignoring the pre-approval for all tasks: changes behaviour for tasks without required review, which the owner kept unchanged.
- Only changing `build_commands.py`: the loop's own early return would still accept the coder approval, so the QA loop would not actually run.

## Risks

- Review-gated resumed builds run one extra QA session (accepted).
- Dropping `qa_signoff` also resets the coder-written `qa_session` count; that count is untrusted anyway.
- `--qa` (`cli/qa_commands.py`) still honours an existing approval; out of scope.

## Verification

New `tests/test_qa_signoff_review_gate.py`:
- build path with a plan carrying `qa_signoff.status=approved`, all subtasks
  completed: `requires_review=True` calls `run_qa_validation_loop`;
  `requires_review=False` does not.
- loop with `requires_review=True` and a pre-approval reaches a reviewer session
  with `qa_signoff` removed; with `requires_review=False` it returns True early.
The tests fail on origin/dev. Plus `tests/test_qa_criteria.py`, `tests/test_run_gates_snapshot.py`, ruff, mypy ratchet.
