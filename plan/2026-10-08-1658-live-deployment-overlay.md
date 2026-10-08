---
status: approved
issue: 1658
spec: spec/2026-10-08-1658-live-deployment-overlay.md
---

# Plan: the deployment overlay holds production on the live merge path

## Approved decisions (carried from the intent and spec)

- **Scope.** Only `merge_disposition` changes
  (`apps/web-server/server/services/pr_endgame.py`, L442–472).
  - `merge_policy.py` is not touched.
  - `apply_path_risk_floor` is not touched.
  - `merge_gate_signals` is not touched.
  - `AIFACTORY_PATH_RISK_FLOOR_ENFORCE` is not touched.
- **Pass the overlay.** `merge_disposition` passes `deployment=` and
  `satisfied_gates=` into `decide_merge`. Both are read the way
  `apply_path_risk_floor` reads them (L253–258):
  - `(load_task_contract(spec_dir) or {}).get("deployment")`;
  - `satisfied_system_gates(spec_dir, deployment)`.
- **Option A (intent decision).** If `context/task_contract.json` exists but
  can't be read or parsed, or its top level isn't a dict, the result is
  `HOLD_BLOCKING_DISPOSITION`.
  - An absent file follows today's path unchanged.
  - The `implementation_plan.json` fallback is not considered.
- **Imports.** `load_task_contract` is imported inside the existing
  `try/except ImportError` in `merge_disposition`. A missing module fails closed
  exactly as a missing `merge_policy` does.
- **Production and high risk always hold.** They are unconditional
  `HOLD_BLOCKING` (spec, "Correction to the intent"). Satisfied gates clear only
  `system_gates` holds.
- **Autonomy matrix.**
  - The generator code is unchanged.
  - The generated md and JSON are regenerated.
  - The TOML claim for `wiring.live_overlay` is reworded.
  - One generator test is corrected so it measures each row's own probe
    directory.

## Steps

1. **`apps/web-server/server/services/pr_endgame.py`: add the helper.**
   Add `_contract_unreadable(spec_dir: Path) -> bool` directly above
   `merge_disposition` (L442). The body:

   ```python
   path = Path(spec_dir) / "context" / "task_contract.json"
   if not path.exists():
       return False
   try:
       return not isinstance(json.loads(path.read_text()), dict)
   except (OSError, ValueError):
       return True
   ```

   Its docstring names #1658 option A, says an absent file is back-compat, and
   notes that `load_task_contract` skips unparseable files on purpose because
   the TFactory handoff relies on that.

   → verify with `apps/backend/.venv/bin/python -c "import ast,sys; ast.parse(open('apps/web-server/server/services/pr_endgame.py').read())"`.

   Traps:
   - `json` is already imported (L22).
   - Use `ValueError`, not `json.JSONDecodeError`, matching `satisfied_system_gates`.
   - The strict ruff/mypy ratchet runs on this file in pre-commit, so give the
     helper full type annotations.

2. **`pr_endgame.py`, `merge_disposition`: wire the overlay.**
   - **Import.** Extend the `try` (L462–463) with
     `from pfactory.tfactory_client import load_task_contract`. Copy the
     `# type: ignore[import-not-found,unused-ignore] # noqa: PLC0415`
     comment exactly as `apply_path_risk_floor` uses it (L220–228).
   - **Unreadable guard.** After the `effective = …` line, add:
     - `if _contract_unreadable(spec_dir):`, then
       `logger.warning("[pr-endgame] task contract unreadable; auto-merge withheld")`
       and `return HOLD_BLOCKING_DISPOSITION`.
     - The log text is a constant. Nothing read off disk goes into the log
       record (py/log-injection, see `_describe_tier`).
   - **Call.** Replace the `return` with:

     ```python
     deployment = (load_task_contract(spec_dir) or {}).get("deployment")
     return str(decide_merge(
         str(effective), **merge_gate_signals(spec_dir),
         deployment=deployment,
         satisfied_gates=satisfied_system_gates(spec_dir, deployment),
     ))
     ```
   - **Docstring.** Add one paragraph: the RFC-0013 overlay is enforced here,
     independent of the path-floor flag (#1658).

   → verify with `cd apps/web-server && ../backend/.venv/bin/python -m pytest -q tests/test_pr_endgame_merge_gate.py`.
   The existing tests stay green.

   Traps:
   - `deployment` may be a non-dict. `decide_merge` and
     `deployment_block_reasons` already return no reasons for a non-Mapping,
     so don't add a guard.
   - Do not touch `merge_gate_signals`.

3. **`apps/web-server/tests/test_pr_endgame_merge_gate.py`: new tests.**
   Add the tests after `test_satisfied_gates_of_an_unreadable_spec_is_empty`
   (L249). Setup for each:
   - green signals: `_spec(tmp_path, tfactoryVerdict="pass", achievedVal=2, valFloor=1, ciParity=True, hostCiGreen=True)`;
   - `monkeypatch.delenv(pe.PATH_RISK_FLOOR_ENV, raising=False)`;
   - tier `"low"`;
   - the contract written to `spec / "context" / "task_contract.json"`, always
     with `"contract_version": "2"`.

   Tests:
   1. `production_classification: production` gives `HOLD_BLOCKING_DISPOSITION`.
   2. `risk_class: high` gives `HOLD_BLOCKING_DISPOSITION`.
   3. `system_gates: ["human-approval"]`, not production:
      - without `satisfiedSystemGates`: hold-blocking;
      - with `satisfiedSystemGates=["human-approval"]`: `AUTO_MERGE_DISPOSITION`.
   4. Contract text `{not json`, and separately `[]`: both give
      `HOLD_BLOCKING_DISPOSITION`.
   5. Production plus `satisfiedSystemGates=["human-approval"]` still gives
      hold-blocking. This pins the correction to the intent.

   → verify with the same pytest command: all pass.
   **Mutation:** temporarily delete `deployment=…, satisfied_gates=…` from the
   call. Tests 1, 2, 3a and 5 must fail. Restore it. Then temporarily delete the
   unreadable guard: test 4 must fail. Restore it. Record both runs in the PR.

   Traps:
   - The constant names are `pe.AUTO_MERGE_DISPOSITION` and
     `pe.HOLD_BLOCKING_DISPOSITION`; check them in the module before use.
   - A contract without a `contract_version` marker is ignored by
     `load_task_contract`, so tests must include the marker.

4. **`docs/compliance/control-objectives.toml` L75: reword the
   `wiring.live_overlay` objective** to:
   "On the live merge path a high risk or production deployment holds the merge
   for a human regardless of the path-floor flag; an unreadable contract holds
   it too."

   → verify with the TOML validator, which runs inside `--check` (step 6).

   Traps:
   - Keep the entry a table with a string `objective`; the validator rejects
     anything else.
   - Leave `evidence` and `frameworks` unchanged.

5. **`tests/test_gen_autonomy_matrix.py`,
   `test_live_overlay_rows_match_the_live_code` (L249–262).** Replace the
   shared contract-less `green` directory. For row `i`, recompute
   `gam.pe.merge_disposition(tmp_path / f"live-{i}", tier)`, which is the probe
   directory `_live_overlay_rows` wrote for that row. Run it with the row's
   enforce value applied through `gam._env(**{gam.pe.PATH_RISK_FLOOR_ENV: val})`,
   where `val` is None for "unset" and `"1"` otherwise.

   → verify with `apps/backend/.venv/bin/python -m pytest -q tests/test_gen_autonomy_matrix.py`.

   Traps:
   - Don't assert a literal disposition here. The test proves that the rows
     equal the live code, not what the policy is.

6. **Regenerate the matrix.** Run
   `apps/backend/.venv/bin/python scripts/gen_autonomy_matrix.py` (writes
   `docs/docs/compliance/autonomy-matrix.md` and
   `docs/static/compliance/autonomy-matrix.json`), then `--check`.

   → verify that `--check` prints `ok: …` and that:
   - all four B5 rows show `hold-blocking`;
   - the "advisory on the live path … AIFactory#1658" sentence is gone;
   - `git diff` touches only B5, D (the reworded objective) and their JSON.

   Traps:
   - Never hand-edit the generated files.
   - The minimum row-count assertions must still hold.

## Tests

- `cd apps/web-server && ../backend/.venv/bin/python -m pytest -q tests/test_pr_endgame_merge_gate.py`:
  all pass, including the 6 new cases.
- `apps/backend/.venv/bin/python -m pytest -q tests/test_gen_autonomy_matrix.py`:
  all pass.
- `apps/backend/.venv/bin/python scripts/gen_autonomy_matrix.py --check`: `ok`.
- The mutation runs from step 3, recorded in the PR.
- Pre-commit (strict ruff/mypy ratchet) passes. CI's required
  `autonomy matrix matches the policy` check is green.

## Hand-off

Steps 1–6 edit five files, so the `coder` agent implements them (one agent,
step 1 first, later steps by SendMessage). A fresh Opus agent reviews, given
only this plan and `git diff`. Merge to `dev`; it ships in the next release.

## Rollback

Revert the PR. It's code plus regenerated docs, with no data or config
migration. After a revert, the next release returns `merge_disposition` to
today's behaviour, and the matrix check forces B5 back with it.

## Deviations (recorded during implementation)

1. **Contract reads that raise now hold** (fresh Opus review, finding N1).
   - **The problem:** step 2 followed `satisfied_system_gates` and caught only
     `(OSError, ValueError)`. Three inputs still raised out of
     `merge_disposition`:
     - a contract nested past Python's recursion limit raises `RecursionError`;
     - a non-UTF-8 `implementation_plan.json` raises `UnicodeDecodeError` from
       `load_task_contract`, which catches only `JSONDecodeError`;
     - a deeply nested `implementation_plan.json` raises `RecursionError` too.

     Nothing merged, but the endgame aborted before the PR was opened.
   - **The fix:** the three reads are wrapped in `except Exception`, which
     holds the merge. That matches how `apply_path_risk_floor` treats the
     same read.
   - **New test:** `test_a_contract_that_raises_on_read_holds_instead`, which
     covers both files with both inputs. Narrowing the catch fails both cases.
2. **Log text** (finding N3). The `ImportError` warning now names the
   task-contract loader as well as `merge_policy`.
3. **Not done here.**
   - Finding N2 (a contract can list its own `satisfied_gates`) predates this
     change and doesn't loosen anything; it's filed as a follow-up.
   - Finding N4 (the generator's now-unreachable advisory branch) is left for
     a later cleanup, consistent with "generator code unchanged".
