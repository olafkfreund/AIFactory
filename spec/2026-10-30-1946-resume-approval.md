---
status: draft
issue: 1946
intent: intent/2026-10-30-1946-resume-approval.md
---

# Spec: re-check approval before the interactive Ctrl+C resume

## Design

In `_handle_build_interrupt`, in the `choice == "skip"` branch, before
`run_autonomous_agent`: if `requires_review_before_coding(spec_dir)` and
`not ReviewState.load(spec_dir).is_approval_valid(spec_dir)`, print a warning
("plan changed since approval; re-approve, then resume") and do not resume;
fall through to the existing "TO RESUME" box (status stays PAUSED). Otherwise
resume as today. Both names are already imported in the module.

`spec_dir` here is the directory the resumed agent snapshots from (the
worktree copy in isolated mode), so the check covers exactly the plan that
would be granted. The source spec dir is not the right one: the agent edits
the worktree copy, and the copy is only synced back on progress, so the source
can still hold the approved plan while the copy is widened. The worktree copy
carries `review_state.json` (`copy_spec_to_worktree` copies the whole dir).

## Alternatives rejected

- Pass the original run's snapshot into the resumed call: needs a new
  `run_autonomous_agent` parameter threaded through `_handle_build_interrupt`
  and `_approved_plan_snapshot`; larger, and silently runs a plan the user may
  not know diverged. The re-check is a few lines and fails closed.
- Honour `--force` on the resume: a forced first run now must re-run
  `run.py --spec <name> --force` to resume after a plan change. Acceptable for
  an interactive path, and keeps the handler free of a new parameter.

## Risks

A legitimate progress-only plan update does not invalidate approval
(`_compute_spec_hash` strips progress keys), so normal resumes are unaffected.

## Verification

New `tests/test_build_interrupt_resume_approval.py`: review-required spec,
approved, then plan widened → choosing "skip" does not call
`run_autonomous_agent`; unchanged approved plan → it is called once.
