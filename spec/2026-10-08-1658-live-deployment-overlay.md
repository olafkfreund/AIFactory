---
status: draft
issue: 1658
intent: intent/2026-10-08-1658-live-deployment-overlay.md
---

# Spec: the deployment overlay holds production on the live merge path

## Correction to the intent

The intent says a recorded `human-approval` "still clears the hold". That's
true only for holds that come from `system_gates`.
`merge_policy.deployment_block_reasons` (L200) treats the two categorical
predicates as **unconditional**:

- `risk_class: high` gives "never auto-merged";
- `production_classification: production` gives "VAL-4 / never autonomous".

Satisfied gates clear only `system_gates` entries. So after this change, a
production or high-risk task is always `hold-blocking`, meaning a human
merges it. That matches `decide_merge` and the module docstring. This spec
doesn't change it.

## Design

All of the change is in `merge_disposition` in
`apps/web-server/server/services/pr_endgame.py` (L442–472). It is the single
function every live merge decision passes through (`run_pr_endgame`, around
L1250).

1. **Import:** `load_task_contract` is imported in the same `try` as
   `decide_merge`. An `ImportError` already returns
   `HOLD_BLOCKING_DISPOSITION` (fail closed), and that now covers the contract
   loader too.
2. **Option A (approved):**
   - A new private helper `_contract_unreadable(spec_dir) -> bool` returns
     True when `context/task_contract.json` exists but reading it raises
     `OSError`, parsing raises `ValueError`, or the top level isn't a dict.
   - When it returns True, `merge_disposition` logs a constant warning and
     returns `HOLD_BLOCKING_DISPOSITION`.
   - An absent file returns False, which keeps the back-compat path.
   - `load_task_contract` itself is unchanged. It skips unparseable files on
     purpose, and the TFactory handoff relies on that.
3. **Pass the overlay:**
   ```python
   deployment = (load_task_contract(spec_dir) or {}).get("deployment")
   return str(decide_merge(
       str(effective), **merge_gate_signals(spec_dir),
       deployment=deployment,
       satisfied_gates=satisfied_system_gates(spec_dir, deployment),
   ))
   ```
   These are the same two readers `apply_path_risk_floor` uses (L253–258), so
   the advisory record and the live decision can't disagree about what the
   contract says.
4. **What stays the same:** `apply_path_risk_floor` and
   `AIFACTORY_PATH_RISK_FLOOR_ENFORCE`. The flag goes back to gating only the
   path floor and its advisory record.

**Autonomy matrix (regenerated, not hand-edited):**
- `scripts/gen_autonomy_matrix.py` needs no code change:
  - B5 already probes `merge_disposition` live;
  - its "advisory on the live path" sentence is emitted only while an unset
    row auto-merges, so it disappears by itself.
- `docs/compliance/control-objectives.toml`: the `wiring.live_overlay` objective
  is a hand-written claim. It is reworded to: "On the live merge path a high
  risk or production deployment holds the merge for a human regardless of the
  path-floor flag; an unreadable contract holds it too."
- `tests/test_gen_autonomy_matrix.py::test_live_overlay_rows_match_the_live_code`
  recomputes each row's disposition against a contract-less directory. After
  this change that comparison is wrong. It must recompute against the row's own
  probe directory (`tmp_path / f"live-{i}"`), which is what the row actually
  measured.

## Alternatives rejected

- **Set `AIFACTORY_PATH_RISK_FLOOR_ENFORCE=1` on the Deployment** (option 2 in
  #1658). It also switches on the path-risk floor for every task, which is a
  broader rollout with its own review. Rejected in the intent.
- **Put `deployment` and `satisfied_gates` into `merge_gate_signals`.** That
  function holds the RFC-0011 *signals*, with "unmeasured means the old
  answer" defaults, and it has no way to say "unreadable, hold". Mixing the
  overlay into it would blur that documented contract.
- **Make `load_task_contract` raise on bad JSON.** It is shared with the
  TFactory handoff, which wants the skip.
- **Fail closed when `implementation_plan.json` is unparseable.** Option A
  names only `context/task_contract.json`. The fallback file is rewritten by
  the executor during every build, so treating it as a contract signal would
  hold ordinary tasks.

## Risks

- **Projects with `AIFACTORY_AUTO_MERGE` on stop auto-merging production,
  high-risk and gate-pending tasks.** This is intended. Live exposure today is
  zero, because the flag is unset on the Deployment.
- **A corrupt `context/task_contract.json` now holds instead of merging.**
  This is intended (option A). A human merges it.
- **The overlay still sees only contracts with RFC-0002 markers.** A
  `deployment` block in a contract without `contract_version`, `tfactory` or
  `approval` is ignored, on both the path floor and now the live path. This
  gap predates this change and is not widened by it. I'll file it as a
  follow-up rather than fold it in.
- **No change in the cluster.** This changes AIFactory code only; it ships in
  the next release.

## Verification

- **New tests in `apps/web-server/tests/test_pr_endgame_merge_gate.py`,** all
  with tier `low`, green signals and the flag unset:
  1. A `production_classification: production` contract gives `hold-blocking`.
  2. A `risk_class: high` contract gives `hold-blocking`.
  3. `system_gates: ["human-approval"]`, not production:
     - `hold-blocking` without a recorded approval;
     - `auto-merge` with `satisfiedSystemGates: ["human-approval"]` in
       `task_metadata.json`.
  4. `context/task_contract.json` containing `{not json` gives `hold-blocking`,
     and so does one containing `[]`.
  5. No contract gives `auto-merge`. The existing
     `test_the_same_task_without_the_handback_still_merges` already pins this.
- **Mutation:** dropping `deployment=` from the call fails tests 1–3, and
  dropping the unreadable guard fails test 4.
- **Matrix:** `scripts/gen_autonomy_matrix.py --check` passes after
  regeneration. B5 shows `hold-blocking` in all four rows, and the
  `AIFactory#1658` sentence is gone.
- **Suites:** `pytest apps/web-server/tests/test_pr_endgame_merge_gate.py
  tests/test_gen_autonomy_matrix.py` is green, and the strict ruff and mypy
  ratchet passes on the touched files.
