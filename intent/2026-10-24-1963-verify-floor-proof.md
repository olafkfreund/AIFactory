---
status: approved
issue: Factory#1963
author: olafkfreund
---

# Intent: prove a passing verdict cannot merge past a red floor

## Problem

TFactory's verify stage decides pass or fail with model judges (best-of-3
votes). AIFactory's merge gate takes that verdict as one input. Factory#1963
asks for a deterministic floor that no verdict can lift, proven by mutation.

The floor exists: `merge.merge_policy.decide_merge` needs host CI green, the
VAL floor met and CI parity on top of a passing verdict
(`apps/backend/merge/merge_policy.py:306-312`). `merge_gate_signals` fails
host CI closed (`apps/web-server/server/services/pr_endgame.py:395-458`).
What is missing is proof at the merge itself. Today's tests check the
`auto_merge` flag the endgame settles on when the PR opens
(`apps/web-server/tests/test_pr_endgame_merge_gate.py:63-158`), or the signal
dict on its own. None of them runs the watcher with a `pass` verdict and a
red floor and checks that `merge_pr` is never called.

## Proposed outcome

One parametrised test drives the real endgame (`run_pr_endgame`, then
`watch_and_finish`, then the merge-time gate) with TFactory returning `pass`
and an approved review. It covers each red floor: host CI not green, achieved
VAL below the floor, CI parity false. In every case `merge_pr` is never
called. A green control case calls `merge_pr` once, so the test cannot pass by
switching merging off. Removing any one floor check from `decide_merge` makes
its case fail.

## Affected users and systems

- AIFactory `apps/web-server/tests/test_pr_endgame_merge_gate.py`, test only.
- No runtime, deployment or host change.

## Constraints

- Test only. Production code changes only if the test exposes a bug, and then
  the plan is updated in the same commit.
- No network: TFactory, `gh` and host CI are stubbed at the seams the module
  already exposes.
- Base `dev`. Hooks are not skipped on code commits.

## Open questions

None. The PFactory slice (verdict and vote split in the run-signature header)
is out of this lane: no verify data reaches `render.py` (see the spec).
