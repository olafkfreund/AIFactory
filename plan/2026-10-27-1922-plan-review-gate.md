---
status: draft
issue: 1922
spec: spec/2026-10-27-1922-plan-review-gate.md
---

# Plan: plan-review gate must not trust files the planner can write

Approved decisions (from the spec):

- The post-planner gate in `run_autonomous_agent` decides from a snapshot taken
  before the first agent session: `require_review` (from `task_metadata.json`)
  and the `ReviewState` (from `review_state.json`). At that point the worktree
  spec dir is a fresh copy of control-plane state on both backends.
- The snapshot approval is valid only if `is_approval_valid(spec_dir)` holds
  against the plan now on disk (hash binding).
- `ReviewState.is_approval_valid` rejects an approval with no `spec_hash`. The only
  hash-less writer (auto-simple in `routes/execution.py`) keeps working through
  `--force`. It no longer counts as the injection-scan escape hatch, which is
  intended.
- No API call from the build. Do not touch
  `apps/web-server/server/services/agent_worktree_sync.py` (#1921 lane).

## Steps

1. `apps/backend/review/state.py` `is_approval_valid` (around line 112-127):
   replace the "Legacy approval without hash - treat as valid" branch so that
   `approved` with an empty `spec_hash` returns False. Update the docstring to say
   that a hash-less approval is never valid (#1922). In
   `tests/test_review_validation.py` change
   `test_is_approval_valid_legacy_no_hash` to assert False and rename it to
   `test_is_approval_valid_rejects_hashless_approval`.
   → verify: `cd apps/backend && .venv/bin/python -m pytest ../../tests/test_review_validation.py -q`
   Traps: run pytest from the repo root if the conftest needs it; don't `git add -A`
   (the `apps/backend/.venv` symlink must never be staged).

2. `apps/backend/agents/coder.py`:
   - Right after `solo = is_solo_mode_enabled_for_spec(spec_dir)` (around line
     212), add the snapshot:
     `require_review = not solo and _should_require_human_review(spec_dir)` and
     `pre_planner_review = ReviewState.load(spec_dir)`, with a one-line comment that
     it is read before any agent session so the planner cannot change the gate (#1922).
     Import `ReviewState` from `review.state` at module scope next to the existing
     `from review.state import requires_review_before_coding` (line 47).
   - At the post-planner gate (around lines 549-559): delete the
     `require_review = ...` recomputation and the deferred `from review import
     ReviewState` / `ReviewState.load(spec_dir)`. Use
     `if require_review and not pre_planner_review.is_approval_valid(spec_dir):`.
     Keep everything inside the pause branch unchanged. Fix the comment above to
     say the decision uses the pre-planner snapshot.
   - Leave `_injection_scan_blocks` alone (it already runs before any agent session).
   → verify: `python -m pytest tests/test_solo_mode.py tests/test_solo_parallel_waves.py -q`
   and `ruff check`/`ruff format --check` on the file, plus `python scripts/ratchet_lint.py`
   if present.
   Traps: the mypy ratchet type-checks coder.py on its own, so `review.state`
   calls may come back as `Any` (see the `bool(...)` note in
   `_should_require_human_review`). Keep the declared types honest the same way.

3. New `tests/test_plan_review_gate_snapshot.py`: drive `run_autonomous_agent`
   with the same mocking harness as `tests/test_solo_mode.py::_run_one_turn`
   (copy it; solo OFF via `AIFACTORY_SOLO_MODE=off`), `max_iterations=2`,
   `is_build_complete` → False, and a `run_session_guarded` side effect that acts
   as the planner: writes `implementation_plan.json` (one phase, one pending
   subtask) and then applies the scenario's forgery, returning
   `("continue", "", {})`. The spec dir starts with `spec.md` and
   `task_metadata.json` `{"requireReviewBeforeCoding": true}`. "Paused" means
   `implementation_plan.json` has `status == "human_review"` and
   `reviewReason == "plan_review"` and the session ran exactly once. Cases:
   - planner rewrites `task_metadata.json` to `{}` → paused;
   - planner writes `review_state.json` `{"approved": true}` → paused;
   - planner writes a `review_state.json` with a correct `spec_hash`
     (`ReviewState().approve(spec_dir)` after writing the plan) → paused;
   - pre-planner `review_state.json` approved for a DIFFERENT plan (approve with
     another plan on disk, then the planner writes a new plan) → paused;
   - pre-planner approval made for exactly the plan the planner then writes
     (byte-identical) → not paused (a second session runs or the plan status is
     not `human_review`);
   - `requireReviewBeforeCoding` false and planner sets it true → not paused
     (proves the snapshot is used in both directions);
   - solo ON (`AIFACTORY_SOLO_MODE=true`) with review required → not paused.
   Trusted-plan fast path: `skipPlanning: true` in metadata enables solo
   (`solo_mode`), so the solo case covers it; add one case with
   `{"requireReviewBeforeCoding": true, "skipPlanning": true}` asserting not paused.
   → verify: `python -m pytest tests/test_plan_review_gate_snapshot.py -q`; then
   revert step 2 locally (`git stash` is NOT allowed — temporarily edit the gate
   back) and confirm the forge/clear cases fail, then restore.
   Traps: `pytest-asyncio` mark as in test_solo_mode; no network; no `git stash`.

## Tests

`python -m pytest tests/test_review_validation.py tests/test_solo_mode.py tests/test_solo_parallel_waves.py tests/test_plan_review_gate_snapshot.py tests/test_injection_ssrf.py -q` → all pass.
`ruff check` + `ruff format --check` on touched Python files.

## Rollback

Revert the PR. No data or schema changes; review_state.json files are unchanged
on disk.
