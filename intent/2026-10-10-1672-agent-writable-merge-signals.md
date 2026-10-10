---
status: approved
issue: 1672
author: olafkfreund
---

# Intent: merge decision trusts agent-writable signals

Follow-up to #1667. Code paths are at commit b753db00.

## Problem

After #1667, a verified trusted task takes its contract, deployment and gates
from the signed, server-held record. The merge decision does not. It still
reads its inputs from `<spec>/task_metadata.json`, and the coding agent can
write that file (`Write`/`Edit(spec_path/**)` and `Bash(*)` in
`apps/backend/core/client.py:755-763`).

- **Review tier.** `gather_pr_context` reads `reviewTier` from that file
  (`pr_endgame.py:1243-1262`, `review_tier_of`). `merge_disposition` then
  calls `resolve_contract` but takes only `deployment` from the verified
  contract. It never reads the signed `execution.review_tier`
  (`pr_endgame.py:~478-492`). The signed tier is copied into metadata once at
  ingest (`trusted_plan.py:711`), and nothing reads it from the record after
  that. The path-risk floor is advisory by default
  (`AIFACTORY_PATH_RISK_FLOOR_ENFORCE` off), so it returns the tier it was
  given. An agent that changes `blocking` to `auto` gets a non-production,
  low-risk task auto-merged.
- **Other merge signals.** `merge_gate_signals` (`pr_endgame.py:337-408`)
  reads `tfactoryVerdict`, `achievedVal`/`valFloor`, `ciParity` and
  `hostCiGreen` from the same file. The issue names these as well. No
  production code writes them, and their defaults already pass, so forging
  them gains nothing today. Trusting a file for these signals is still the
  same flaw.
- **Handback receipt.** `handback_received.json` is written in the spec dir
  by `qa.correction.record_handback`. The QA Fixer agent then runs in that
  same dir, so it can delete the receipt, and the endgame then sees no
  handback.
- **Same tier, other readers.** The same tier also feeds
  `_clears_with_human_approval` (`pr_endgame.py:824-836`) and the pre-handoff
  floor in `completion_orchestration.py:209-235`. TFactory picks its VAL floor
  from the tier in that pre-handoff call. `_record_path_risk_floor` writes the
  tier back into the agent-writable file.

**Exposure today: latent, not live.** Auto-merge requires `host_isolated()`,
meaning kubejob plus the job-state store. On kubejob the agent edits a copy of
the spec dir inside the Job. The control-plane `task_metadata.json` is not
mounted there, and nothing syncs it back. On hosts where the agent can write
the real file, `merge_disposition` already holds. The hole opens if any of
these happens:

- #1680 lands and makes sandbox-pidns count as isolated
- a change syncs metadata back from the build
- a new writer changes `reviewTier` after ingest

## Proposed outcome

- The merge decision for a verified trusted task never depends on a value the
  coding agent can write. Lowering `reviewTier`, forging a gate signal, or
  deleting the handback receipt cannot turn a hold into an auto-merge.
- `merge_disposition` and `_clears_with_human_approval` use the same
  effective tier.
- Changes only tighten. A missing or unreadable input is decided no more
  loosely than today. Today an absent `reviewTier` is decided as `low`, and an
  unmeasured gate signal takes its passing default.
- Tests show that a tampered `task_metadata.json` does not change the merge
  disposition of a verified task.

## Affected users and systems

- `apps/web-server/server/services/pr_endgame.py` (`gather_pr_context`,
  `review_tier_of`, `apply_path_risk_floor`, `merge_disposition`,
  `merge_gate_signals`, `_clears_with_human_approval`)
- `completion_orchestration.py` (pre-handoff floor, TFactory tier),
  `trusted_contract.py` (`resolve_contract`), `merger.py` (tier shown in the
  PR body)
- `apps/backend/trusted_plan.py`, `apps/backend/merge/merge_policy.py`,
  `apps/backend/qa/correction.py`
- Operators with `AIFACTORY_AUTO_MERGE` on, mainly kubejob deployments, and
  sandbox-pidns once #1680 lands
- TFactory, if the tier it receives changes
- The autonomy matrix (#1962), which must be regenerated if the merge-policy
  surface changes

## Constraints

- **Trust boundary.** The merge decision may only read inputs the agent
  cannot reach, given `Bash(*)`. Moving or renaming a file inside the spec
  dir, the worktree or `.aifactory/` does not count. Allowed sources are the
  signed `TrustedRecord`, the job-state DB, GitHub and TFactory.
- **Only tighten.** Follow the merge policy's existing rule: never lower a
  tier, never treat an unmeasured signal as a pass, fail closed to hold.
  `AIFACTORY_AUTO_MERGE` stays the master switch.
- **Keep the #1667 states** (`verified` / `hold` / `legacy`). Legacy and
  from-issue tasks have no signed tier. A verified contract may lack
  `execution.review_tier`, and that case must not fall back silently to the
  metadata value.
- **One record lookup.** Reuse the `trusted` object that completion
  orchestration already passes along. Do not add a second DB read or a path
  built from a request-supplied spec id.
- **Log hygiene.** Any tier value still goes through `_describe_tier` /
  `sanitize_log` before it reaches a log or the PR body (py/log-injection).
- **Metadata stays.** `task_metadata.json` keeps its legitimate writers and
  its non-merge readers.
- **No local state.** New server-side state lives in the shared store. With
  no `DATABASE_URL`, behaviour must be no looser than today.
- **No #1680 dependency.** Do not rely on #1680. Changing the default of
  `AIFACTORY_PATH_RISK_FLOOR_ENFORCE` affects every project and needs an
  explicit decision.
- **Tests stay green.** Existing #1667, #1658 and #1663 tests and
  `test_merge_gate` stay green. The autonomy-matrix `--check` gate must pass.
- **Conflicts.** PR #1691 (#1680) touches the same files.

## Open questions

1. **Scope:** `reviewTier` only (as the #1667 review comment asks), or every
   signal the issue lists, plus the handback receipt?
2. **Other signals:** for VAL, parity, verdict and host-CI, keep the
   defaults, re-fetch them from TFactory and GitHub, or stop reading them
   from metadata?
3. **Handback receipt:** record it somewhere the QA Fixer agent cannot
   reach?
4. **Verified tasks:** take the signed `execution.review_tier`, always
   enforce the path floor, or both?
5. **Verified contract with no signed tier:** hold, use the path floor, or
   keep the metadata value?
6. **Legacy and from-issue tasks:** accept the risk, enforce the floor, or
   hold?
7. **Floor default:** should `AIFACTORY_PATH_RISK_FLOOR_ENFORCE` default to
   on, globally or per trust state?
8. **TFactory:** must the corrected tier also reach TFactory's VAL-floor
   choice before handoff?
9. **Write-back:** should `_record_path_risk_floor` keep writing `reviewTier`
   into the agent-writable file?
10. **Order:** land before or after PR #1691, and wait for the post-#1680
    revisit of `host_isolated()`?
11. **Tier mismatch:** when the metadata tier differs from the signed tier,
    log it or flag it on the PR as possible tampering, or override it
    silently?
