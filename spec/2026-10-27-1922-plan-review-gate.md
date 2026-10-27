---
status: draft
issue: 1922
intent: intent/2026-10-27-1922-plan-review-gate.md
---

# Spec: plan-review gate must not trust files the planner can write

## Design

1. **Snapshot before any agent turn** (`apps/backend/agents/coder.py`, next to
   `solo = is_solo_mode_enabled_for_spec(spec_dir)`): compute
   `require_review = not solo and _should_require_human_review(spec_dir)` and
   `pre_planner_review = ReviewState.load(spec_dir)` once, before the first
   session. At that point the worktree spec dir is a fresh copy of the
   control-plane spec dir on both backends (see intent) and no agent has run.
   The post-planner gate uses only these snapshots:
   `if require_review and not pre_planner_review.is_approval_valid(spec_dir)`.
   `is_approval_valid` still hashes the CURRENT `spec.md` +
   `implementation_plan.json`, so the snapshot approval counts only if it was
   made for exactly the plan the planner left on disk. In practice that means
   a fresh plan always pauses for review, which is the intended behaviour.
2. **No hash, no approval** (`apps/backend/review/state.py`
   `is_approval_valid`): `approved` without `spec_hash` returns False.

Who still writes hash-less approvals (checked on origin/dev):

- `routes/execution.py` simple-task fast path writes
  `{"approved": true, "approved_by": "auto-simple"}`. The start route reads the
  raw `approved` key and passes `--force`, so `run.py`'s pre-flight is bypassed
  as before (it now prints the existing "Bypassing approval check" warning for
  quick-mode tasks). The plan-review gate never runs for these tasks (the plan
  already exists, so no planner session). The only behaviour change: an
  auto-simple stamp no longer acts as the injection-scan escape hatch
  (`_injection_scan_blocks`), so an injection-flagged simple task now pauses for
  a human. That hatch is documented as an operator approval, so this is the
  intended behaviour.
- `scripts/fix-stuck-tasks.py` writes `approved: false`; unaffected.
- `trusted_plan._mark_review_approved` and `approve-plan` use
  `ReviewState.approve`, which always sets a hash.

No API call from the build is needed: the snapshot comes from control-plane
state before the agent can write anything, so there is nothing for the planner
to tamper with.

## Alternatives rejected

- **Read the gate from `source_spec_dir`.** It is None in direct mode, and on
  kubejob it lives in the same emptyDir as the agent. The snapshot covers both
  without branching.
- **Ask the web-server API from the build.** The Job has no API credential.
  Adding one widens the Job's secrets and adds a network failure mode for no gain
  over the snapshot.
- **Hash binding alone.** The MD5 `spec_hash` is computable by the agent, so a
  forged file with a correct hash would still pass. Only the snapshot timing
  closes the hole.
- **Make the auto-simple stamp carry a hash.** That would keep an automatic
  stamp working as the injection-scan escape. Rejected for a security fix.

## Risks

- A plan-review task whose planner leaves the plan byte-identical to an
  approved one would proceed. That is correct: the human approved that plan.
- Injection-flagged simple tasks now pause (see above). Both backends.
- Cross-process forgery via the main spec dir (subprocess path mirroring
  worktree files) is AIFactory#1921, out of scope.

## Verification

New tests driving `run_autonomous_agent` through one planner turn (mocked
session) with plan review required:

- planner clears `requireReviewBeforeCoding` → still pauses (plan
  `status: human_review`, no second session);
- planner writes `{"approved": true}` and a hashed forged `review_state.json`
  → still pauses;
- a real approval of a different plan hash in the pre-planner state → still pauses;
- a real approval matching the plan on disk → proceeds;
- solo / trusted-plan (`skipPlanning`) → no pause, unchanged.

Plus `test_review_validation.py`: a hash-less approval is invalid. Run
`pytest tests/test_review_validation.py tests/test_solo_mode.py` and the new file.
