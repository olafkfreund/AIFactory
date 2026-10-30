---
status: draft
issue: 1946
spec: spec/2026-10-30-1946-resume-approval.md
coders: [agy]
---

# Plan: re-check approval before the interactive Ctrl+C resume

Approved decisions (from the spec):

- Re-check with `requires_review_before_coding(gate_dir)` and
  `ReviewState.load(gate_dir).is_approval_valid(spec_dir)`, where
  `gate_dir = source_spec_dir or spec_dir` (revised after review: the worktree
  copy of the gate files is agent-writable).
- Invalid → warning, no resume, fall through to the existing "TO RESUME" box.
- `_handle_build_interrupt` gains `source_spec_dir`; no snapshot passing.

## Steps

1. `apps/backend/cli/build_commands.py` `_handle_build_interrupt`, `if choice == "skip":`
   branch: guard the resume with the check above (one-line `# #1946` comment
   on the why), using the module's existing imports and `print_status(..., "warning")`.
   Add `tests/test_build_interrupt_resume_approval.py` (sys.path setup and
   spec/plan helpers as in `tests/test_plan_review_gate_snapshot.py`):
   monkeypatch `cli.build_commands.select_menu` to return `"skip"`,
   `cli.build_commands.StatusManager` and `print_paused_banner` with
   MagicMocks, `agent.run_autonomous_agent` with an AsyncMock. Case A: approve
   (`ReviewState().approve(spec_dir)`), then widen the plan (add
   `required_commands`) → mock not awaited, no SystemExit. Case B: approved,
   unchanged → mock awaited once, SystemExit(0).
   Deviation (lead, step 1): the check moved into a predicate
   `_approval_still_valid(spec_dir)` joined to the existing `if choice == "skip"`,
   because an extra branch in `_handle_build_interrupt` tripped the strict-ruff
   ratchet (PLR0912).
   → verify: `cd apps/backend && python -m pytest ../../tests/test_build_interrupt_resume_approval.py ../../tests/test_plan_review_gate_snapshot.py -q` and `ruff check` + `ruff format --check` on both files.

2. (Revision after the lead's review; lead.) Pass `source_spec_dir` from
   `handle_build` into `_handle_build_interrupt` and `_approval_still_valid`;
   read the gate files from `source_spec_dir or spec_dir`. Tests: forged
   worktree `review_state.json` → no resume; flipped worktree
   `task_metadata.json` → no resume; approved source + unchanged worktree
   plan → resumes.
   → verify: as step 1.

## Rollback

Revert the commit; the resume returns to today's behaviour.
