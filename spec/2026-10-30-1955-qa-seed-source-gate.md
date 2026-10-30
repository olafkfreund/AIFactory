---
status: draft
issue: 1955
intent: intent/2026-10-30-1955-qa-seed-source-gate.md
---

# Spec: QA plan-command seeding reads the approval gate from the source spec dir

## Design

In `handle_qa_command`, set `gate_dir = get_specs_dir(project_dir) / spec_dir.name`
(the same lookup `find_spec` makes first, so it equals `spec_dir` on the normal
path). Seed the live plan (`plan=None`) only when `gate_dir` is a directory
and either `not requires_review_before_coding(gate_dir)` or
`ReviewState.load(gate_dir).is_approval_valid(spec_dir)`; otherwise `plan={}`.
`is_approval_valid` keeps hashing `spec_dir`, the plan actually seeded.

## Alternatives rejected

- Pass `source_spec_dir` from `cli/main.py`: `main` does not know it; it would
  recompute the same path.
- Change `find_spec` to stop the worktree fallback: merge/review/discard rely
  on it.
- Seed from the source plan: the source plan is the synced worktree plan on
  the normal path anyway, and is missing on the fallback path.

## Risks

A spec that exists only in a worktree and needs plan commands for QA no
longer gets them; QA still runs and reports the denied command.
Revised after the lead's review: `--project-dir` pointed at the worktree
root (`<root>/.aifactory/worktrees/tasks/<name>`) made `gate_dir == spec_dir`
and failed open. `project_dir` inside `.aifactory/worktrees/` now resolves to
the source project root when the path is exactly that layout; any other path
under `.aifactory/worktrees/` fails closed (`plan={}`).

## Verification

`tests/test_profile_untrusted.py`: the existing parametrised test moves its
spec under `.aifactory/specs/`; new cases with real `ReviewState`: approved
source plan, worktree copy with a widened plan and (a) a forged worktree
`review_state.json`, (b) worktree metadata flipped to
`requireReviewBeforeCoding: false` → seeded with `{}`; normal source path,
approved and unchanged → seeded with `None`. Ratchet and full suite green.
