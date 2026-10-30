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

Revised after the lead's review: the gate inputs must not come from the
worktree copy. `copy_spec_to_worktree` copies `review_state.json` and
`task_metadata.json` into the agent-writable worktree, and the spec hash is an
unkeyed MD5, so the agent could re-approve its widened plan there or set
`requireReviewBeforeCoding: false`. So:

- `_handle_build_interrupt` takes `source_spec_dir` (the canonical spec dir,
  `None` outside worktree mode) from `handle_build`.
- `requires_review_before_coding` and `ReviewState.load` read
  `source_spec_dir or spec_dir`; `is_approval_valid` still hashes the worktree
  `spec_dir`, the plan the resumed agent snapshots.

Second revision after review: the resumed `run_autonomous_agent` read
`require_review` (and solo) from the worktree `task_metadata.json`, so a
flipped flag with an unchanged plan passed the resume check and then ran with
no snapshot, granting any later mid-run `required_commands` addition (#1940
again). `run_autonomous_agent` now reads that gate from
`source_spec_dir or spec_dir`, and the resume passes `source_spec_dir`. The
snapshot is still parsed from the worktree plan at entry, which the resume
check has just matched to the source approval. No new parameter: the resume
call gains the `source_spec_dir` the first call already passes. A first run
is unchanged (the copy equals the source at setup).

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
`run_autonomous_agent`; unchanged approved plan → it is called once; with a
separate source dir, a worktree `review_state.json` re-approving the widened
plan → no resume; worktree `task_metadata.json` flipped to no review → no
resume; `run_autonomous_agent` with flipped worktree metadata and an approved
source → a mid-run `required_commands` addition is not granted; the resume
passes `source_spec_dir`.
