---
status: approved
issue: 1672
intent: intent/2026-10-10-1672-agent-writable-merge-signals.md
---

# Spec: merge decision trusts agent-writable signals

Code paths are at commit c9e8fce3 (the intent's b753db00 plus the intent commits).

## Design

For a verified trusted task, `merge_disposition` takes the review tier from the
signed contract. The tier read from `task_metadata.json` and the path floor can
only raise it. Nothing else changes.

This needs one guarded block in one function and a fixture change. It reuses
three existing helpers: `resolve_contract`, `raise_review_tier` and
`_describe_tier`. No new module, flag, store or public API is added.

### Why `merge_disposition`

Both merge callers already go through it:

- the main merge path, `merge_pr` (`pr_endgame.py:1411-1413`)
- `_clears_with_human_approval` (`pr_endgame.py:823-836`)

One guard here gives both the same effective tier, which the intent requires.
`resolve_contract` already returns the signed record's contract at
`pr_endgame.py:484`. For a verified task that is `trusted.contract`
(`trusted_contract.py:86-87`), and it holds the whole signed plan, `execution`
block included (`routes/execution.py:1456-1458`, `trusted_plan.py:784-785`).
So no second record lookup is needed.

### Change 1: `apps/web-server/server/services/pr_endgame.py`, `merge_disposition` (:428-506)

a) The existing lazy import of `decide_merge` (:461) also imports
`raise_review_tier`. If that import fails, the existing `ImportError` branch
already holds.

b) Inside the existing `try` (:483-493), right after `deployment` is read, a
block that runs only when `state == "verified"`:

- Read the signed tier from `contract["execution"]["review_tier"]`, treating a
  missing or non-dict `execution` as no tier.
- If `_describe_tier(signed)` is `none` or `unrecognised`, log a constant
  warning ("verified contract has no signed review_tier") and return
  `HOLD_BLOCKING_DISPOSITION`. `_TIER_LOG_LABEL` has the same six keys as
  `merge_policy._TIER_ALIASES`, so this is exactly the set `decide_merge`
  cannot rank. A non-string signed tier makes `_describe_tier` raise, and the
  existing `except` turns that into `unreadable` and HOLD.
- If the incoming tier is non-blank and ranks below the signed tier, log one
  warning ("reviewTier X below signed Y"), with both values passed through
  `_describe_tier` and `sanitize_log`.
- Unless the effective tier is unrecognised, set it to
  `raise_review_tier(signed, effective)`. An unrecognised incoming tier is
  passed through unchanged so `decide_merge` holds it, as today.
  `raise_review_tier` would rank it -1 and return the signed tier, which could
  turn today's hold into an auto-merge.

Legacy and from-issue tasks (`state == "legacy"`) never enter the block, so
their behaviour is unchanged.

### How each input is decided (verified tasks)

| Signed `execution.review_tier` | Incoming tier (metadata, after path floor) | Result | Logged |
|---|---|---|---|
| missing, or no `execution` block | any | HOLD | "no signed review_tier" |
| unrecognised string, e.g. `"bogus"` | any | HOLD | "no signed review_tier" |
| not a string, e.g. `1` or a dict | any | HOLD, via the existing `except` → `unreadable` | "task contract unreadable" |
| `blocking` | `auto` / `low` (forged) | decided on `blocking` → HOLD | "below signed" |
| `async` | `low` | decided on `async` → `hold-async` | "below signed" |
| `auto` | `blocking` (path-floor raise) | decided on `blocking` → HOLD | nothing |
| `auto` | `None` or blank | decided on `auto` | nothing |
| `auto` | `"bogus"` | passed through → HOLD, as today | "below signed" |
| `auto` | `low` / `LOW` (same bucket) | decided on `auto` | nothing |

Why the extra checks are needed:

- `raise_review_tier(tier, floor)` ranks an unrecognised value as -1
  (`merge_policy.py:118-127`). Without the signed-tier check, a garbage signed
  tier would let a metadata `auto` win.
- Without the pass-through for an unrecognised incoming tier, a `"bogus"`
  metadata tier would rank below a signed `auto` and auto-merge. Today
  `decide_merge` holds it (`merge_policy.py:288-291`). That would break the
  only-tighten rule.
- A blank incoming tier is not logged, so kubejob builds, which never sync
  metadata back, do not warn on every run.

Only constants from `_describe_tier` reach the log (py/log-injection).

### Change 2: `apps/web-server/tests/test_trusted_contract_merge.py`

- `_plan` (:56-79) gains `review_tier: str | None = "auto"`. When the tier is
  set, it writes `plan["execution"] = {"review_tier": review_tier}`. `_spec`
  (:91-104) passes it through. Test 16 (:215-218) then auto-merges on a signed
  `auto`, as Q5 requires.
- New tests, all on a verified record unless noted:
  - signed `blocking`, tier `low`: HOLD, and caplog shows the "below signed"
    warning. This is the forged-tier case.
  - signed `blocking`, tier `auto`: `_clears_with_human_approval(...)` returns
    False.
  - signed `async`, tier `low`: `hold-async`.
  - signed `auto`, tier `blocking`: HOLD, and no "below signed" warning.
  - `review_tier=None` (signed plan with no `execution` block): HOLD.
  - signed `"bogus"` and signed `1` (parametrized): HOLD.
  - signed `auto`, tier `None` and `""`: auto-merge, with no warning.
  - signed `auto`, tier `"bogus"`: HOLD (unchanged).
  - signed `auto`, tier `"auto\nFORGED"`: the text `FORGED` never appears in
    `caplog.text`.
- Legacy tests 17a and 17b (:221-233) stay unchanged and must still pass.

That is two files and about 20 lines of production code.

### Intent outcomes this change does not meet

The intent's first outcome also covers forged gate signals and a deleted
handback receipt. Under Q1 this change leaves both to follow-ups:

- **Gate signals.** Forging them cannot loosen the decision today, because
  every unmeasured default already passes (`pr_endgame.py:377-387`).
- **Handback receipt.** Deleting `handback_received.json`
  (`pr_endgame.py:389-390`, `:406-407`) still removes a HOLD on a host where
  the QA Fixer shares the control-plane spec dir. On kubejob it does not.
  This stays open until the follow-up moves the receipt to the job-state DB.

Approving this spec accepts that gap. The PR must say so and link the
follow-up.

### Decisions on the intent's open questions

Each item is the default I chose. Please confirm or change it.

1. **Scope:** `reviewTier` only, fixed once inside `merge_disposition` for
   verified tasks. Follow-up issues cover the gate signals (Q2), the handback
   receipt (Q3), legacy tasks (Q6), the write-back (Q9) and the tier shown in
   the PR body (`merger.py:256-272`). This is the only forgeable input that
   turns a hold into an auto-merge today.
2. **Other signals (VAL, parity, verdict, host-CI):** keep reading them from
   metadata, as today. Re-fetching them from TFactory and GitHub is a
   follow-up. Forging them gains nothing today (`pr_endgame.py:357-408`), and
   dropping the read would also drop a recorded negative such as
   `ciParity: false`, which would be looser.
3. **Handback receipt:** not in this change. A follow-up records it in the
   job-state DB. Moving it needs a new store write (`qa/correction.py:63-98`)
   and a matching read (`pr_endgame.py:394-407`). Moving it inside the spec dir
   does not count as a fix.
4. **Verified tasks:** use the signed `execution.review_tier`, raised by the
   incoming tier through `raise_review_tier(signed, tier)`. Floor enforcement
   stays as it is. A tampered tier cannot loosen the result, and a legitimate
   path-floor raise still applies.
5. **Verified contract with no signed tier:** HOLD. The intent forbids a silent
   fallback to metadata, and the path floor alone could return `auto`.
6. **Legacy and from-issue tasks:** accept the risk and leave them unchanged.
   A follow-up revisits this once #1680 or a metadata sync-back lands. The
   exposure is latent, and changing every project needs an explicit decision.
7. **`AIFACTORY_PATH_RISK_FLOOR_ENFORCE` default:** keep it off. The fix does
   not depend on it.
8. **TFactory VAL floor:** no change for verified tasks. The handoff already
   sends the signed contract (`completion_orchestration.py:243-245`,
   `trusted_contract.py:63-68`). Legacy tasks get a follow-up.
9. **Write-back in `_record_path_risk_floor`:** keep it. It can only raise the
   tier (`pr_endgame.py:313-325`), and removing it would take the enforced
   floor away from legacy tasks and from TFactory.
10. **Order:** land before PR #1691 without waiting for it. Whichever PR lands
    second rebases.
11. **Tier mismatch:** when the incoming tier ranks below the signed tier, log
    one warning with constant labels and decide on the stricter tier. Flagging
    it on the PR is a follow-up.

## Alternatives rejected

- **Pass the signed tier to `raise_review_tier` without checking it.** A
  garbage signed tier ranks -1, so the metadata tier wins and the hole stays
  open.
- **Decide on the signed tier alone and ignore the incoming tier.** This
  throws away an enforced path-floor raise, which is looser (against Q4).
- **Fall back to the metadata tier or the path floor when no signed tier
  exists.** This is the silent fallback the intent forbids, and the floor alone
  can return `auto` (Q5).
- **Fix the tier in `gather_pr_context` or `apply_path_risk_floor` (:1327), or
  in each caller.** That is two or more places to keep in step.
  `_clears_with_human_approval` and any future caller would still rely on the
  metadata read.
- **Re-read the tier from `TrustedContractStore` or `context/task_contract.json`.**
  This is a second lookup. The on-disk copy is only trusted because
  `_record_verified` compares it, and `trusted.contract` is already in hand.
- **Import `merge_policy._TIER_ALIASES`, or add a public `normalize_tier`.** The
  first uses a private name across packages, and the second adds API surface.
  `_TIER_LOG_LABEL` already holds the same six keys.
- **Move `reviewTier`, the handback receipt or the gate signals out of the
  agent's reach, enforce the floor by default, or show the tier in the PR
  body.** Out of scope under Q1-Q3, Q7 and Q9. Each goes to a follow-up.

## Risks

1. **Signed plans without `execution.review_tier` stop auto-merging.** They
   hold every time (Q5, intended). v1 contracts are accepted with no execution
   block (`trusted_plan.py:64-66`), and the old test fixture had none. The
   PFactory intake path does set the tier (`intake/execution_block.py:178`).
   Plans signed any other way hold until they are re-signed. The warning line
   makes this visible.
   - Before landing, sample recent verified tasks with
     `jq '.execution.review_tier' <spec>/context/task_contract.json` and confirm
     the PFactory signer always sets it.
2. **Verified tasks get stricter.** A signed `async` with metadata `auto`, or a
   metadata tier that was lowered or left blank, now holds where it used to
   auto-merge. This is intended, and it shows up as the new warning, not as a
   PR flag.
3. **The tier tables are coupled.** If a new spelling is added to
   `_TIER_ALIASES` but not to `_TIER_LOG_LABEL`, verified tasks with that tier
   hold. That fails closed, never open.
4. **The warning can fire twice per build.** Both callers run
   `merge_disposition`, so a downgrade is logged twice. Accepted rather than
   adding dedupe state.
5. **Rebase with #1691.** Only the `host_isolated()` lines next to this block
   (:475-481) may conflict. The logic is independent (Q10).
6. **Autonomy matrix (#1962).** The merge-policy surface does not change,
   but CI's `--check` gate must still pass.
7. **Hosts.** Only hosts with `AIFACTORY_AUTO_MERGE` on and `host_isolated()`
   true are affected: kubejob deployments today, and sandbox-pidns once #1680
   lands. Other hosts already hold.

## Verification

```
cd /mnt/code/Source-home/GitHub/AIFactory-1672
apps/backend/.venv/bin/pytest -q -o asyncio_mode=auto \
  apps/web-server/tests/test_trusted_contract_merge.py \
  apps/web-server/tests/test_pr_endgame_review_tier.py \
  apps/web-server/tests/test_pr_endgame_path_risk_floor.py \
  apps/web-server/tests/test_merge_decision_reaches_the_merge.py
GRAPHITI_ENABLED=false APP_DISABLE_AUTH=true \
  apps/backend/.venv/bin/pytest apps/web-server/tests -q -o asyncio_mode=auto   # ci.yml:214-218
ruff check apps/web-server/server/services/pr_endgame.py
```

Done means:

- the forged `blocking`-to-`low` test holds, and so does the
  `_clears_with_human_approval` variant;
- the missing, unrecognised and non-string signed-tier tests hold;
- test 16 auto-merges on the signed `auto`, and tests 17a and 17b are unchanged;
- the existing #1667, #1658 and #1663 tests, `test_merge_gate` and the
  autonomy-matrix `--check` gate pass;
- the log-injection test passes and CodeQL `py/log-injection` stays clean;
- mutation check: removing the `raise_review_tier(signed, effective)` step
  makes the forged-tier test fail;
- the follow-up issues for Q1-Q3, Q6, Q8 (legacy) and Q11 (PR flag) are filed
  and linked in the PR.
