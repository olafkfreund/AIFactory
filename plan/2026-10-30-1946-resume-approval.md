---
status: approved
issue: 1946
spec: spec/2026-10-30-1946-resume-approval.md
coders: [agy]
---

# Plan: re-check approval before the interactive Ctrl+C resume

Approved decisions (from the spec):

- Re-check with `requires_review_before_coding(spec_dir)` and
  `ReviewState.load(spec_dir).is_approval_valid(spec_dir)` on the `spec_dir`
  the handler already has (the copy the resume snapshots from).
- Invalid → warning, no resume, fall through to the existing "TO RESUME" box.
- No new parameters; no snapshot passing.

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
   → verify: `cd apps/backend && python -m pytest ../../tests/test_build_interrupt_resume_approval.py ../../tests/test_plan_review_gate_snapshot.py -q` and `ruff check` + `ruff format --check` on both files.

## Rollback

Revert the commit; the resume returns to today's behaviour.
