---
status: approved
issue: 1924
intent: intent/2026-10-27-1924-approval-hash-content.md
---

# Spec: hash the reviewed plan content, not the plan file bytes

## Design

In `apps/backend/review/state.py`, `_compute_spec_hash()` keeps hashing `spec.md` raw,
and hashes the plan through a new `_compute_plan_hash()`:

1. Parse `implementation_plan.json`. If it is missing, unreadable, not JSON or not an
   object, fall back to the raw `_compute_file_hash()` (today's behaviour).
2. Drop runtime progress keys (a denylist, so unknown keys stay hashed — fail closed):
   - plan level: `status`, `planStatus`, `reviewReason`, `recoveryNote`,
     `qa_signoff`, `qa_iteration_history`, `qa_stats`, `last_updated`,
     `updated_at`, `phase` (the web-server's `spec_creation` marker; the plan's
     structure lives in `phases`).
   - subtask level, in each phase's `subtasks` and legacy `chunks`: `status`,
     `notes`, `updated_at`, `started_at`, `completed_at`, `actual_output`,
     `critique_result`.
3. Hash `json.dumps(content, sort_keys=True)`.

Writers of these keys found on dev: `routes/plan_approval.py`, `routes/execution.py`,
`routes/tasks.py`, `services/task_status.py`, `services/agent_service.py`,
`services/agent_worktree_sync.py`, `agents/tools_pkg/tools/subtask.py`,
`agents/tools_pkg/tools/qa.py`, `agents/session.py`, `agents/coder.py`,
`services/recovery.py`, `qa/report.py`, `cli/build_commands.py`.

The approve route is unchanged: it only rewrites ignored keys, so hashing before the
rewrite is now correct. This fits #1926: StartTask still forces only on
`is_approval_valid(main)` or `planApproved`; the first now stays True as intended.

## Alternatives rejected

- Recompute the hash after the route's rewrite (issue's first option): fixes only
  the route; the coder's status writes still invalidate mid-build.
- Allowlist the reviewed fields: a new plan field the coder acts on would change
  without invalidating the approval (fails open).
- Store the approval only in `task_control`: #1926 already does that for StartTask;
  run.py, QA and the injection-scan check read `ReviewState` and are out of scope.

## Risks

- An ignored key that does carry reviewed meaning would let that change pass. The
  list holds only status/progress/timestamp/output keys written by the runtime.
- Existing stored hashes (raw bytes) no longer match: those tasks pause once for
  re-approval, or resume through `planApproved`. Fail closed.

## Verification

- New tests in `tests/test_review_validation.py`: approve, then apply the route's
  rewrite and subtask status/notes/timestamps → still valid; change a subtask
  description, add an unknown key, change `feature` → invalid; non-JSON plan still
  hashed. The "still valid" test fails on dev.
- `test_spec_change_detection_accuracy` asserted that a subtask status change
  invalidates; it now changes a subtask description instead.
- `apps/web-server/tests/test_plan_approval_resume.py` passes unchanged.
