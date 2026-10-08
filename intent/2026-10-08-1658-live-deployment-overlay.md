---
status: approved
issue: 1658
author: olafkfreund
---

# Intent: the deployment overlay holds production on the live merge path

## Problem

`merge_policy.decide_merge` holds any production or `risk_class: high` change
(`HOLD_BLOCKING`, the RFC-0013 overlay), but only when the deployment block is
passed in. The live path never passes it:

- `pr_endgame.merge_disposition` calls
  `decide_merge(effective, **merge_gate_signals(spec_dir))`, and
  `merge_gate_signals` carries neither `deployment` nor `satisfied_gates`.
- The overlay reaches the live decision only through `apply_path_risk_floor`.
  That function applies the floor only when `AIFACTORY_PATH_RISK_FLOOR_ENFORCE`
  is set. Otherwise it records the floor as advisory and returns the tier
  unchanged.

Measured on `dev` on 2026-10-08: a production contract with tier `low`, all
signals green and no high-risk path gives `auto-merge` with the flag unset (as
on the live Deployment), and `hold-blocking` with the flag set to `1`.

Today nothing auto-merges, because `AIFACTORY_AUTO_MERGE` is off. Any project
that turns it on would auto-merge production and high-risk changes, which
contradicts the module's own rule: "Production is VAL-4 and never autonomous".
The published autonomy matrix (section B5) now states this gap in public.

## Proposed outcome

- With auto-merge on and `AIFACTORY_PATH_RISK_FLOOR_ENFORCE` unset, the live
  `merge_disposition` returns `hold-blocking` for a production or high-risk
  deployment whose blocking gates are not satisfied. That is the same answer
  `decide_merge` already gives.
- A recorded `human-approval` (or any other declared satisfied gate) still
  clears the hold, exactly as it does in `decide_merge`.
- The path-risk floor rollout flag goes back to its original job, which is
  gating the path floor only.
- The regenerated autonomy matrix shows B5 as `hold-blocking` in every row, and
  its "advisory on the live path" note is removed.

## Affected users and systems

- AIFactory `apps/web-server/server/services/pr_endgame.py`
  (`merge_gate_signals` or `merge_disposition`) and its tests.
- The generated autonomy matrix and JSON (regenerated; the required check
  forces this).
- Projects that enable `AIFACTORY_AUTO_MERGE`. Production and high-risk tasks
  they currently auto-merge will be held. That is the intended tightening.
- No change to `merge_policy.py`.

## Constraints

- **Tighten only.** The overlay may only make a decision stricter. A task with
  no deployment block must decide exactly as today (back-compat).
- **Reuse, don't duplicate:**
  - `load_task_contract` and `satisfied_system_gates` already produce both
    inputs for `apply_path_risk_floor`;
  - the live call must use the same two functions, so the two paths can't
    disagree.
- The fix must not turn on the path-risk floor. That is a separate, broader
  rollout (option 2 in #1658, rejected here).
- **Measure the fix both ways.**
  - The B5 rows flip to `hold-blocking` with the flag unset.
  - A production task with `human-approval` satisfied still auto-merges.
  - A task without a contract is unchanged.

## Open questions

1. **What happens when the contract exists but can't be read?**
   `apply_path_risk_floor` treats it as "adds nothing" (fail-open).
   `merge_disposition` already fails closed when the policy can't be imported.
   - **A (recommended):** fail closed only when `context/task_contract.json`
     exists but can't be parsed. An absent file stays back-compat. "We couldn't
     read the production flag" is not "it isn't production".
   - **B:** match the path floor's fail-open behaviour, for consistency.

## Decision on approval (2026-10-08)

1. **Option A.** A `context/task_contract.json` that exists but can't be parsed
   holds the merge (`hold-blocking`). An absent contract stays back-compat.
