---
status: draft
issue: 1958
intent: intent/2026-10-30-1958-solo-from-source.md
---

# Spec: a resumed run reads solo mode from the source spec dir

## Design

In `run_autonomous_agent` (`apps/backend/agents/coder.py`), move
`gate_dir = source_spec_dir or spec_dir` above the solo line and compute
`solo = is_solo_mode_enabled_for_spec(gate_dir)` once;
`require_review = not solo and _should_require_human_review(gate_dir)`
(the pre-#1952 shape, now on `gate_dir`). The plan snapshot stays on
`spec_dir`. `cli/build_commands.py:314` needs no change: it reads solo before
the worktree is created, so `spec_dir` there is the source.

## Alternatives rejected

- Copy-protect `task_metadata.json` in the worktree: wider change, and the
  agent can still write the dir.
- Leave `solo` on `spec_dir`: the issue.

## Risks

A legitimate per-run solo change made only in the worktree copy is ignored;
nothing writes it there by design.

## Verification

New test in `tests/test_plan_review_gate_snapshot.py`: source with solo off,
worktree copy with `soloMode: true`, env override unset, no plan → the first
session gets the planner prompt, not the solo prompt. Fails on today's code.
Ratchet and full suite green.
