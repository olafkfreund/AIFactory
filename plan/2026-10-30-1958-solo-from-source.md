---
status: draft
issue: 1958
spec: spec/2026-10-30-1958-solo-from-source.md
coders:
  1: codex  # the lead's brief names Codex for this lane
---

# Plan: a resumed run reads solo mode from the source spec dir

Approved decisions (from the spec): `solo` and `require_review` both come from
`gate_dir = source_spec_dir or spec_dir`; the plan snapshot stays on
`spec_dir`; `cli/build_commands.py` is unchanged.

## Steps

1. `apps/backend/agents/coder.py` ~207-218: set `gate_dir` first,
   `solo = is_solo_mode_enabled_for_spec(gate_dir)`,
   `require_review = not solo and _should_require_human_review(gate_dir)`;
   keep one comment citing #1922/#1946/#1958. In
   `tests/test_plan_review_gate_snapshot.py` add
   `test_resume_solo_comes_from_source_not_worktree_metadata`: `monkeypatch.delenv("AIFACTORY_SOLO_MODE")`,
   source `_make_spec(tmp_path, {"requireReviewBeforeCoding": False})`, worktree
   = `shutil.copytree(source, tmp_path / "worktree")` with
   `_write_meta(worktree, {"soloMode": True})`, `_drive(worktree, tmp_path,
   _planner(PLAN_A), source_spec_dir=source)` → first session's prompt
   (`m.await_args_list[0].args[1]`) is `"PLANNER_PROMPT"`.
   → verify: `cd apps/backend && .venv/bin/python -m pytest ../../tests/test_plan_review_gate_snapshot.py ../../tests/test_build_interrupt_resume_approval.py -q`; ruff check + format on both files; CI ratchet.
   Traps: strict-ruff ratchet (no new branches in `run_autonomous_agent`); the
   autouse `_solo_off` fixture sets the env override, so the test must delete it.

## Tests

The step's check, then the full backend suite as CI runs it.

## Rollback

Revert the commit.
