---
status: approved
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
   Restart without approval (added after plan approval at the lead's request;
   no code change, coverage only). On a restart the plan already exists, so no
   planner runs and the coder gate is not reached. The guard is run.py's
   pre-flight (`cli/build_commands.handle_build_command`, `ReviewState.load(spec_dir)`
   on the MAIN spec dir before `setup_workspace`). Restarts that do not pass force
   (recover route, process_monitor auto-restarts) get no `--force`, because
   `services/task_phase.should_pass_force` = `force or not require_review`. Add:
   - `handle_build_command(..., auto_continue=True, force_bypass_approval=False)`
     with a REAL `ReviewState`, the plan on disk, `requireReviewBeforeCoding: true`,
     and review_state.json (a) absent, (b) `{"approved": true}` (hash-less),
     (c) approved for a different plan → `SystemExit(0)` and
     `agent.run_autonomous_agent` not awaited; (d) a valid approval for the plan
     on disk → `run_autonomous_agent` awaited (reuse the patch list from
     `tests/test_solo_mode.py::test_solo_forces_skip_qa_in_build_command`, minus the
     ReviewState mock);
   - `should_pass_force(spec_dir, False)` is False when review is required
     (import from `server.services.task_phase`, web-server on sys.path as in
     test_solo_mode).
   → verify: `python -m pytest tests/test_plan_review_gate_snapshot.py -q`; then
   revert step 2 locally (`git stash` is NOT allowed — temporarily edit the gate
   back) and confirm the forge/clear cases fail, then restore.
   Traps: `pytest-asyncio` mark as in test_solo_mode; no network; no `git stash`.

Implementation deviations (step 3):
- An existing plan skips the planner, so the "different plan" / "exact plan"
  cases approve a plan and then delete it (`_approve_then_remove_plan`) so a
  planner session runs and the gate is reached.
- The `skipPlanning` case unsets `AIFACTORY_SOLO_MODE` (the env override beats
  the metadata flag).
- The valid-approval restart case patches `build_commands.count_subtasks` to
  `(1, 1)` so the silent no-op guard does not exit 1 after the mocked agent.
- Step 2 uses one combined `if` (the cq ratchet rejects the nested form as SIM102).
- After the Opus review: restart-blocked cases also assert the agent was not
  awaited; "not paused" cases assert the plan was never stamped (`_proceeded`)
  instead of `not _paused`; the now-redundant deferred `ReviewState` import in
  `_injection_scan_blocks` and its stale docstring reference were removed.

## Steps added after the lead's review of PR #1926 (resume path)

The decision (lead, 2026-10-27): StartTask (`apps/web-server/server/routes/execution.py`)
must pass `--force` only on a real approval. Its current check, the raw `approved`
key of main `review_state.json` at ~681-690, lets a hash-less or stale approval
resume straight into coding: `--force` skips run.py's pre-flight, and with the plan
present no planner runs, so the coder gate is never reached.

The lead first proposed a "coding started" marker. It was rejected because it is
forgeable: the only signal for it is the stdout phase marker `__EXEC_PHASE__:`,
which `agent_emit._parse_phase_event` accepts unauthenticated. The model's text
is printed to stdout (`agents/session.py:525`). This is a separate issue, not
fixed here.

4. Done by me (no coder):
   - `routes/execution.py` auto-simple fast path writes a hashed approval
     (`ReviewState().approve(spec_dir, approved_by="auto-simple")`).
   - `cli/build_commands.py` pre-flight comment and warning no longer claim the
     coder re-checks approval.
   - `ruff format tests/test_review_validation.py`.
5. `apps/web-server/server/services/task_control.py` `write_control`: add
   keyword `plan_approved: bool | None = None`. True sets `planApproved: true`,
   `planApprovedBy` (= `updated_by`) and `planApprovedAt`; False removes all
   three. Rule inside `write_control`: writing `review_reason="plan_review"`
   always removes them, so every return-to-planning path clears the approval
   in one place.
   → verify: unit test in `apps/web-server/tests/` for set / clear / cleared by plan_review.
6. `apps/web-server/server/routes/plan_approval.py`: `approve_plan` calls
   `task_control.write_control(spec_dir, plan_approved=True, updated_by="web_user")`.
   Do it unconditionally, right after `review_state.approve(...)`, NOT inside the
   `if plan_file.exists()` try (keep the existing status write where it is).
   `reject_plan` calls `write_control(spec_dir, plan_approved=False, ...)`.
   → verify: route tests (existing approve/reject tests plus asserts on task_control).
7. `apps/web-server/server/routes/execution.py` StartTask (~681-690):
   `force_execution = ReviewState.load(spec_dir).is_approval_valid(spec_dir) or
   task_control.read_control(spec_dir).get("planApproved") is True`. Keep the
   log line. The backend is on sys.path (line ~56), so
   `from review.state import ReviewState` works.
   → verify tests: hash-less approval + Start → human_review and no
   start_task_execution; valid approval + Start → started with force; planApproved
   + plan changed (stale hash) + Start → started; auto-simple plan + approval →
   `is_approval_valid` True.
7b. (Added while implementing; the lead's "every return-to-planning path" rule.)
   On subprocess, whether the exit path writes `reviewReason=plan_review` depends on
   process_monitor reading the main `review_state.json`. So the approval is also
   cleared at the one entry every start path uses:
   `AgentService.start_task_execution` clears `planApproved` when
   `prompts_pkg.prompts.is_first_run(main spec_dir)` holds, i.e. a planner is about
   to run and will replace the approved plan. Tests in
   `apps/web-server/tests/test_plan_approval_resume.py`.
   Deviation (step 7): the step 4 function-local `ReviewState` import in
   `start_task` made the name local to the whole function (UnboundLocalError on
   the new force check). It is now a module-level import beside the other backend
   seams.
8. After #1923 merges: `git merge origin/dev` (no force-push, no rebase), then a
   test showing that a planner-written `review_state.json` / `task_metadata.json`
   in the worktree spec dir is not mirrored to main by the sync, and the next
   StartTask decision is unchanged.

Not changed: process_monitor's raw `approved` reads (:327, :428). They only
decide whether an exit is shown as "awaiting review" or "failed", and never
pass force.

## Tests

`python -m pytest tests/test_review_validation.py tests/test_solo_mode.py tests/test_solo_parallel_waves.py tests/test_plan_review_gate_snapshot.py tests/test_injection_ssrf.py -q` → all pass.
`ruff check` + `ruff format --check` on touched Python files.

## Rollback

Revert the PR. No data or schema changes; review_state.json files are unchanged
on disk.
