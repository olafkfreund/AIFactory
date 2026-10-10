---
status: approved
issue: 1672
spec: spec/2026-10-10-1672-agent-writable-merge-signals.md
---

# Plan: merge decision trusts agent-writable signals

Line numbers were checked against the worktree at `b0712214`. That checkout is
on `dev` and already includes #1691, so they are 8 lines later than the spec's
`c9e8fce3` citations.

## Approved decisions (self-contained)

**The problem.** The coding agent can write `task_metadata.json`. Today
`merge_disposition` decides auto-merge from that file's `reviewTier`. An agent
that rewrites `blocking` to `low` therefore turns a hold into an auto-merge,
even on a task whose plan PFactory signed.

**The fix (D-design, Q1, Q4).** For a *verified* trusted task,
`merge_disposition` takes the review tier from the signed contract
(`contract["execution"]["review_tier"]`). The incoming tier, from metadata and
the path floor, can only raise it. Nothing else changes. The change is one
guarded block in one function plus a test-fixture change. It reuses
`resolve_contract`, `raise_review_tier` and `_describe_tier`. There is no new
module, flag, store or public API.

**Where (D-site).** The block goes in `merge_disposition`
(`apps/web-server/server/services/pr_endgame.py:436-515`), because both merge
callers go through it:

- `merge_pr` (:1418-1421)
- `_clears_with_human_approval` (:831-844)

Both callers inherit the guard with no change. `resolve_contract`
(`trusted_contract.py:86-87`) already returns `("verified", trusted.contract)`,
and that contract is the whole signed plan, including `execution`. No second
record lookup is needed.

**Behaviour inside `merge_disposition`:**

- **D-a.** The existing lazy import of `decide_merge` also imports
  `raise_review_tier`. If the import fails, the existing `ImportError` branch
  returns HOLD.
- **D-b1.** The block runs only when `state == "verified"`. It sits inside the
  existing `try`, right after `deployment` is read. The signed tier is
  `contract["execution"]["review_tier"]`. A missing or non-dict `execution`
  means there is no signed tier.
- **D-b2.** If `_describe_tier(signed)` is `"none"` or `"unrecognised"`, the
  block logs a constant warning ("verified contract has no signed review_tier")
  and returns `HOLD_BLOCKING_DISPOSITION`. `_TIER_LOG_LABEL` (:163-170) has the
  same six keys as `merge_policy._TIER_ALIASES` (`merge_policy.py:63-70`), so
  this is exactly the set that `decide_merge` cannot rank. A non-string signed
  tier makes `_describe_tier` raise; the existing `except` sets `unreadable`,
  and the function returns HOLD.
- **D-b3.** If the incoming tier is non-blank and ranks below the signed tier,
  the block logs one warning, "reviewTier X below signed Y". Both values pass
  through `sanitize_log(_describe_tier(...))`. A blank incoming tier is not
  logged, because kubejob never syncs metadata back.
- **D-b4.** Unless the effective tier is unrecognised, the block sets
  `effective = raise_review_tier(signed, effective)`. An unrecognised incoming
  tier passes through unchanged, so `decide_merge` holds it as it does today.
  `raise_review_tier` ranks an unrecognised value at -1 and would return the
  signed tier, which would turn a hold into an auto-merge.
- **D-legacy, Q6.** Legacy and from-issue tasks (`state == "legacy"`) never
  enter the block, so their behaviour does not change. Accepting that risk is a
  follow-up.
- **D-log.** Only constants returned by `_describe_tier` reach the log
  (CodeQL py/log-injection).
- **Q5.** A verified contract with no signed tier HOLDs. It never falls back
  to the metadata or path-floor value.
- **Q7.** `AIFACTORY_PATH_RISK_FLOOR_ENFORCE` stays off by default; the fix
  does not depend on it.
- **Q9.** The write-back in `_record_path_risk_floor` stays: it can only raise
  the tier.
- **Q8.** There is no TFactory change: the handoff already sends the signed
  contract.
- **Q11.** On a mismatch, the block logs one warning and decides on the
  stricter tier. Flagging the mismatch on the PR is a follow-up.
- **Q10.** This lands before or after PR #1691. #1691 is already merged in this
  checkout, and its `host_isolated` lines (:485-489) are adjacent. Do not touch
  them.

**Decision table (D-table).** "signed" is the tier in the signed contract;
"incoming" is the `tier` passed to `merge_disposition`.

| signed | incoming | result | log |
|---|---|---|---|
| missing / no `execution` | any | HOLD | no signed review_tier |
| unrecognised string (`bogus`) | any | HOLD | no signed review_tier |
| non-string (`1`, dict) | any | HOLD (except → unreadable) | task contract unreadable |
| blocking | auto / low | HOLD | below signed |
| async | low | `hold-async` | below signed |
| auto | blocking (path-floor raise) | HOLD | none |
| auto | None / `""` | `auto-merge` | none |
| auto | `bogus` | HOLD (passed through) | below signed (label `unrecognised`) |
| auto | low / LOW | `auto-merge` | none |

**Known gap (D-gap, Q2, Q3).** This change does not meet two of the intent's
outcomes:

- Forged gate signals (VAL, parity, verdict, host-CI) are still read from
  metadata. Forging them gains nothing today. Dropping the read would also drop
  recorded negatives such as `ciParity: false`, which would loosen the gate.
- A deleted handback receipt still removes a HOLD where QA Fixer shares the
  control-plane spec dir. This does not happen on kubejob. The follow-up stores
  the receipt in the job-state DB.

The PR must state this gap and link the follow-ups for Q1-Q3, Q6, Q8 (legacy),
Q11 (PR flag), and the tier shown in the PR body (`merger.py:256-272`).

**Rejected alternatives:**

- Passing the signed tier to `raise_review_tier` unchecked. A garbage signed
  tier ranks -1, so the hole stays open.
- Deciding on the signed tier alone. That drops the path-floor raise.
- Falling back to metadata or the path floor when there is no signed tier.
- Fixing it in `gather_pr_context`, in `apply_path_risk_floor`, or in each
  caller.
- Re-reading the tier from `TrustedContractStore` or
  `context/task_contract.json`.
- Importing `merge_policy._TIER_ALIASES`, or adding a public `normalize_tier`.
- Moving reviewTier, the handback receipt or the gate signals; enforcing the
  floor by default; or showing the tier in the PR body. All are out of scope.

**Risks:**

1. Signed plans without `execution.review_tier` stop auto-merging. This is
   intended. Before landing, sample
   `jq '.execution.review_tier' <spec>/context/task_contract.json` on recent
   verified tasks, and confirm that the PFactory signer always sets the field
   (`intake/execution_block.py:178`).
2. Verified tasks become stricter. The new warning makes this visible.
3. The coupling between `_TIER_ALIASES` and `_TIER_LOG_LABEL` fails closed.
4. The warning may be logged twice per build, once per caller. This is
   accepted; there is no dedupe.
5. A rebase with #1691 only touches the `host_isolated` lines.
6. The autonomy-matrix (#1962) `--check` gate must still pass.
7. Only hosts with `AIFACTORY_AUTO_MERGE` on and `host_isolated()` true are
   affected.

**Hand-off.** There are 3 file-editing steps across 3 files, so `coder` does
the work. Start one `coder` with this plan path and step 1. Send steps 2 and 3
to the same agent with `SendMessage`. A fresh `model: "opus"` agent then
reviews, given only this plan path and the `git diff`.

## Steps

Environment for every command:

```
cd /mnt/code/Source-home/GitHub/AIFactory-1672
export PATH=/mnt/code/Source-home/GitHub/AIFactory/apps/backend/.venv/bin:$PATH
```

1. **`apps/web-server/tests/test_trusted_contract_merge.py:9-17, 56-104, append after :330`: fixture change plus new tests (test file only).**
   → verify by
   `python -m pytest apps/web-server/tests/test_trusted_contract_merge.py -q -o asyncio_mode=auto`.
   The expected result is **red** only for tests 21, 22, 23, 25, 26[bogus] and
   26[1], which auto-merge or miss the log before the fix. Everything else is green, including 16, 17a, 17b and the existing HOLD tests
   1-6, 9-11, 14, 15 and 18.
   - **Imports, :9-17.** Add `import logging` in the stdlib block, between
     `import json` and `import sys`.
   - **`_plan`, :56.** Change the signature to
     `_plan(deployment: dict[str, Any] | None = None, review_tier: object = "auto")`.
     The type is `object` because the int case passes through it. Before
     `return plan`, add
     `if review_tier is not None: plan["execution"] = {"review_tier": review_tier}`.
   - **`_spec`, :91-104.** Add the keyword `review_tier: object = "auto"` after
     `tier`, and call `_sign(_plan(deployment, review_tier=review_tier))` at
     :95. `_spec` overwrites `meta["reviewTier"]` after ingest, so the signed
     value never reaches metadata through the fixture. `ingest_trusted_plan`
     accepts `'auto'`, `'bogus'`, `1` and `'blocking'` (all `ok=True`).
   - **Leave unchanged:** test 16 (:215-218), which now auto-merges on the
     signed `auto`; 17a and 17b (:221-233); and tests 19, 20 and the
     isolation tests.
   - **Append at the end of the file**, after
     `test_a_record_never_verifies_on_a_non_isolated_host`. For each test,
     build with `spec, signed = _spec(tmp_path, review_tier=...)` and call
     `pe.merge_disposition(spec, <tier>, trusted=_rec(signed))`. Where a test
     checks the log, call `caplog.set_level(logging.WARNING, logger=pe.logger.name)`
     first; test 29 uses `logging.DEBUG`.

     | test | signed | incoming | asserts |
     |---|---|---|---|
     | `test_21_forged_low_below_signed_blocking_holds` | `blocking` | `"low"` | `== HOLD`; `"below signed" in caplog.text` |
     | `test_22_human_approval_cannot_clear_signed_blocking` | `blocking` | `"auto"` | `pe._clears_with_human_approval(spec, "auto", _rec(signed)) is False` |
     | `test_23_forged_low_below_signed_async_holds_async` | `async` | `"low"` | `== "hold-async"` |
     | `test_24_tier_above_signed_is_kept_without_warning` | `auto` | `"blocking"` | `== HOLD`; `"below signed" not in caplog.text` |
     | `test_25_verified_contract_without_signed_tier_holds` | `None` | `"low"` | `== HOLD`; `"no signed review_tier" in caplog.text` |
     | `test_26_unusable_signed_tier_holds`, parametrized `signed_tier` over `["bogus", 1]` | param | `"low"` | `== HOLD` |
     | `test_27_blank_tier_takes_signed_auto`, parametrized `tier` over `[None, ""]` | `auto` | param | `== "auto-merge"`; `"below signed" not in caplog.text` |
     | `test_28_unrecognised_tier_not_raised_to_signed` | `auto` | `"bogus"` | `== HOLD` |
     | `test_29_forged_tier_never_reaches_the_log` | `auto` | `"auto\nFORGED"` | `== HOLD`; `"FORGED" not in caplog.text` |

     Tests 28 and 29 assert nothing about "below signed". An unrecognised
     incoming tier ranks -1, so it does log "below signed", with the label
     `unrecognised`.

   Traps:
   - The repo runs `ruff format --check` and `ruff check` on this file.
   - Keep `import logging` unaliased, on its own line, in the stdlib block.
   - The new `_plan` and `_spec` signatures exceed 88 columns on one line; run
     `ruff format` on the file before `ruff format --check`.
   - Never put `# noqa` on a wrapped import.
   - Number the new tests from 21 on, following the spec's numbering.

2. **`apps/web-server/server/services/pr_endgame.py:469, :500, :457-460`: the guard in `merge_disposition` (production).**
   → verify by running the step-1 pytest command (now **fully green**), then
   `ruff format --check apps/backend apps/web-server scripts tests && ruff check apps/web-server/server/services/pr_endgame.py`,
   then, after `git add`, both CI ratchets (`--config` is required; the bare
   `--staged` form exits with a usage error):
   `python scripts/cq_ratchet.py --staged --ruff "$(command -v ruff)" --config standards/ruff.toml --paths 'apps/backend/*.py' 'apps/web-server/*.py' 'scripts/*.py'`
   and
   `python scripts/cq_ratchet.py --tool mypy --staged --mypy "$(command -v mypy)" --config standards/mypy.ini --paths 'apps/backend/*.py' 'apps/web-server/*.py' 'scripts/*.py'`
   (both must report `0 regressed`). Finally, run the mutation check: delete the `effective = raise_review_tier(signed, effective)`
   line, run the step-1 command, and confirm that tests 21, 22 and 23 fail.
   Then restore the line.
   - **:469.** Change the import to
     `from merge.merge_policy import decide_merge, raise_review_tier  # noqa: PLC0415`.
     Keep it on one line (86 columns) with no alias. The `ImportError` branch
     at :476-482 stays as it is.
   - **After :500** (`deployment = contract.get("deployment")`), inside the
     existing `try`, add:
     ```python
             if state == "verified":
                 execution = contract.get("execution")
                 signed = (
                     execution.get("review_tier") if isinstance(execution, dict) else None
                 )
                 if _describe_tier(signed) in ("none", "unrecognised"):
                     logger.warning(
                         "[pr-endgame] verified contract has no signed review_tier; "
                         "auto-merge withheld"
                     )
                     return HOLD_BLOCKING_DISPOSITION
                 if (
                     tier is not None
                     and str(tier).strip()
                     and raise_review_tier(tier, signed) != tier
                 ):
                     logger.warning(
                         "[pr-endgame] reviewTier %s below signed %s; "
                         "deciding on the stricter tier",
                         sanitize_log(_describe_tier(tier)),
                         sanitize_log(_describe_tier(signed)),
                     )
                 if _describe_tier(effective) != "unrecognised":
                     effective = raise_review_tier(signed, effective)
     ```
     `raise_review_tier(tier, signed) != tier` is true only when the signed
     tier ranks strictly above the incoming one. Pairs in the same bucket, such
     as `auto`/`low` and `async`/`medium`, do not log. No new helper is needed.
     A non-string signed tier raises `AttributeError` in `_describe_tier`; the
     `except` at :501-504 sets `unreadable = True`, and :505-507 returns HOLD.
   - **Docstring, :457-460**, at the end of the back-compat paragraph. Add:
     "For a verified trusted task the signed `execution.review_tier` is the
     floor; metadata and the path floor can only raise it, and a verified
     contract without one holds."

   Traps:
   - Only constants from `_describe_tier` reach the log (py/log-injection).
     Never log `tier` or `signed` directly.
   - Keep the `# noqa: PLC0415` import on one line. If formatting wraps it, the
     default ruff config and `standards/ruff.toml` disagree.
   - The block must stay **inside** the `try`. Outside it, `1` escapes as an
     `AttributeError` instead of failing closed.
   - Do not change the `host_isolated` lines (:485-489, from #1691) or the
     `effective = ...` default at :490.
   - Do not touch `_TIER_LOG_LABEL`/`_describe_tier` (:163-177),
     `_clears_with_human_approval` (:831-844), the merge block in `merge_pr` (:1418-1442),
     `merge_policy.py` or `trusted_contract.py`.
   - No subprocess is added, so `test_no_unscrubbed_spawn` does not apply.

3. **`CHANGELOG.md:3` (`## [Unreleased]` → `### Security`): one new bullet, at the top of the list, then the full verification.**
   → verify by running the commands in **Tests** below.
   - The bullet opens with **"A verified trusted task's review tier now comes
     from its signed contract (#1672)."** It then says three things:
     `task_metadata.json` and the path floor can only raise the tier; a
     verified contract without a signed `execution.review_tier` holds; and a
     mismatch logs a warning. It ends with the gap: forged gate signals, the
     handback receipt and legacy/from-issue tasks are not yet covered, with
     the follow-up issue numbers listed.

   Traps:
   - The commit scope must not contain `#`. Use
     `fix(merge): take the review tier from the signed contract (#1672)`, and
     cite the plan steps in the body.
   - If `gen_autonomy_matrix.py --check` reports stale output, run it without
     `--check` and commit the regenerated output. No change is expected: the
     import closure for `server.services.pr_endgame` has a minimum of 176
     (`_MIN_CLOSURE`, `gen_autonomy_matrix.py:478`), `raise_review_tier` comes
     from a module already in the closure, and the matrix cites no
     `pr_endgame.py` line numbers (`--check` stayed `ok` with this plan's
     change applied).
   - Before the PR:
     - Run the risk-1 `jq` sample and confirm the PFactory signer sets
       `execution.review_tier`.
     - File the follow-ups for Q1-Q3, Q6, Q8 (legacy), Q11 (PR flag) and the
       PR-body tier.
     - The PR links the intent, spec, plan and those follow-ups, states the
       D-gap, and says which steps `coder` did.
     - CodeQL py/log-injection must be clean.
   - Any deviation from these steps updates this plan in the same commit as
     the code.

## Tests

```
python -m pytest -q -o asyncio_mode=auto \
  apps/web-server/tests/test_trusted_contract_merge.py \
  apps/web-server/tests/test_pr_endgame_review_tier.py \
  apps/web-server/tests/test_pr_endgame_path_risk_floor.py \
  apps/web-server/tests/test_merge_decision_reaches_the_merge.py \
  apps/web-server/tests/test_pr_endgame_merge_gate.py
GRAPHITI_ENABLED=false APP_DISABLE_AUTH=true python -m pytest apps/web-server/tests -q -o asyncio_mode=auto
python -m pytest tests -q -m "not slow"     # test_merge_policy, test_gen_autonomy_matrix
python scripts/gen_autonomy_matrix.py --check
ruff format --check apps/backend apps/web-server scripts tests
ruff check apps/backend apps/web-server scripts tests
python scripts/cq_ratchet.py --staged --ruff "$(command -v ruff)" --config standards/ruff.toml \
  --paths 'apps/backend/*.py' 'apps/web-server/*.py' 'scripts/*.py'
python scripts/cq_ratchet.py --tool mypy --staged --mypy "$(command -v mypy)" --config standards/mypy.ini \
  --paths 'apps/backend/*.py' 'apps/web-server/*.py' 'scripts/*.py'
```

**Expected results:**

- `test_trusted_contract_merge.py`: the baseline is 21 passed. The change adds
  9 functions, which collect as 11 ids (26 and 27 are parametrized). The
  expected total is 32 passed; count the collected ids rather than trusting a
  fixed number.
- Every other suite passes with no new failures.
- The matrix prints
  `ok: tiers=10 overlay=12 val=8 paths=28 gates=3 controls=13`, unchanged.
- ruff and the ratchet are clean.

**Mutation checks.** Apply each mutation to `pr_endgame.py` one at a time, run
the trusted-contract file, then revert. Back the fixed file up with `cp` first
and restore from that copy. **Never use `git stash`** for this: the stash is
shared by every worktree of the repo, and parallel agents run in sibling
worktrees, so a `stash pop` can take another agent's WIP (this happened while
this plan was being checked).

| # | mutation | must fail |
|---|---|---|
| M1 | delete `effective = raise_review_tier(signed, effective)` | 21, 22, 23 |
| M2 | delete the `return HOLD_BLOCKING_DISPOSITION` in the no-signed-tier branch | 25, 26[bogus] |
| M3 | drop the `_describe_tier(effective) != "unrecognised"` guard | 28, 29 |
| M4 | log raw `tier`/`signed` | 29 |
| M5 | invert the below-signed comparison, or warn always | 21, or 24/27 |
| M6 | run the block for every state, not only `verified` | 17a |
| M7 | move the block outside the `try` | 26[1] (errors) |
| M8 | revert the `_plan` fixture to no `execution` | 16 (also 21, 23, 27) |
| M9 | import only `decide_merge` | 16, 21, 23, 27[None], 27[""] (the `NameError` is caught by the existing `except Exception` and holds, so HOLD-expecting tests still pass; `ruff check` F821 also flags it) |

## Rollback

The change is one commit touching `pr_endgame.py`,
`test_trusted_contract_merge.py` and `CHANGELOG.md`. It has no schema,
migration, flag or store change. Roll back with `git revert <sha>`, then rerun
the Tests block and the matrix `--check`.

**What a rollback does:** verified tasks decide from the metadata `reviewTier`
again, which reopens the forged-tier hole.

**If HOLDs pile up after deploy** because the PFactory signer omits
`execution.review_tier`: revert this commit or make PFactory sign the field.
Never loosen the guard.
