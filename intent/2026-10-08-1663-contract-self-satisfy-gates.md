---
status: draft
issue: 1663
author: olafkfreund
---

# Intent: a task contract cannot clear its own system gates

## Problem

RFC-0013 lets a task contract require system gates before its change may
auto-merge (`deployment.system_gates`, e.g. `["human-approval"]`). The live
merge path asks `merge.merge_policy.deployment_block_reasons`
(`apps/backend/merge/merge_policy.py:200`) which required gates are still
outstanding. It takes that answer from `satisfied_system_gates`
(`apps/web-server/server/services/pr_endgame.py:318`), called from
`merge_disposition` (`pr_endgame.py:500`) and from the path-risk floor
(`pr_endgame.py:258`).

`satisfied_system_gates` counts a gate as cleared when it appears in either
of two places:

1. the contract's own `deployment.satisfied_gates` (`pr_endgame.py:327-330`);
2. `satisfiedSystemGates` / `satisfied_system_gates` in the spec's
   `task_metadata.json` (`pr_endgame.py:315`, `:331-341`).

A contract that says both `system_gates: ["human-approval"]` and
`satisfied_gates: ["human-approval"]` therefore auto-merges with no human
involved. The gate both demands an approval and supplies it.

Who writes each source, and so which trust boundary each one crosses:

- **The contract is authored upstream, before the code exists.** The
  `/from-plan` route (`apps/web-server/server/routes/execution.py:1334`,
  ingest at `:1433`) calls `ingest_trusted_plan`
  (`apps/backend/trusted_plan.py:803`), which verifies the HMAC signature and
  writes the plan verbatim to `implementation_plan.json` and
  `context/task_contract.json` (`trusted_plan.py:845`). The signature covers
  everything except the `approval` envelope (`_plan_core`,
  `trusted_plan.py:157`), so `deployment.satisfied_gates` is signed by the
  planning authority (PFactory's key holder) at plan-approval time. The
  approval it records was given to a plan. Nobody has approved the diff that
  will actually merge, because that diff did not exist yet.
- **The merge path does not re-verify the signature.** `load_task_contract`
  (`apps/backend/pfactory/tfactory_client.py:87`) reads
  `context/task_contract.json` and falls back to `implementation_plan.json`
  (`:104-105`) as plain JSON. `_contract_unreadable`
  (`pr_endgame.py:443`) checks only that the JSON parses.
- **The coding agent can write both sources during the build.** The agent's
  SDK permissions grant `Write`/`Edit` on `{spec_dir}/**`
  (`apps/backend/core/client.py:756-757`). That directory holds
  `context/task_contract.json`, `implementation_plan.json` and
  `task_metadata.json`. So the agent that wrote the change can, at merge
  time, add `satisfied_gates`, add `satisfiedSystemGates` to the metadata, or
  remove `system_gates` or `production_classification` from the contract
  altogether. The signature check at ingest does nothing to stop this.
- **No trusted writer of the metadata key exists.** Nothing in `apps/`
  outside `pr_endgame.py` and tests writes `satisfiedSystemGates`. The comment
  at `pr_endgame.py:313-314` says "an operator (or an upstream approval step)"
  records it, but no such step exists. PR approvals are never read, although
  `decide_merge`'s docstring (`merge_policy.py`, `satisfied_gates` argument)
  gives "an `human-approval` recorded on the PR" as the example. The only
  working way to clear a gate today is self-declaration.

Scope of the harm, as stated in the issue: `risk_class: high` and
`production_classification: production` are unconditional reasons
(`merge_policy.py:233-242`), so `satisfied_gates` alone cannot clear them.
Only `system_gates` holds are affected. The agent-writable contract file is
the exception: it reaches past that limit (see open question 3).

The published autonomy matrix states `wiring.live_overlay` as "holds the
merge for a human" (`docs/docs/compliance/autonomy-matrix.md:179`). That
claim is weaker than it reads.

## Proposed outcome

- A required system gate counts as satisfied only on evidence recorded
  **after** the change exists, by a party other than the contract author and
  the coding agent. A contract that both demands and declares
  `human-approval` is held for a human (`HOLD_BLOCKING`).
- Neither source the coding agent can write during its build can turn a hold
  into an auto-merge.
- `human-approval` is satisfiable at all: a real human approval, recorded
  somewhere the merge path reads, clears it. Without this every
  `human-approval` contract stays held forever.
- The `wiring.live_overlay` row in the autonomy matrix says "for a human"
  with no caveat, and that statement is true.

## Affected users and systems

- `apps/web-server/server/services/pr_endgame.py`: `satisfied_system_gates`,
  `merge_disposition`, the path-risk floor.
- `apps/backend/merge/merge_policy.py`: `deployment_block_reasons` and
  `decide_merge`, as consumers. Their contract to callers may change.
- `apps/backend/pfactory/tfactory_client.py`: `load_task_contract` (shared
  with the TFactory handoff and the prompt builders in
  `apps/backend/prompts_pkg/prompts.py`).
- `apps/backend/trusted_plan.py` and `apps/backend/core/client.py`: the
  contract stash and the agent's spec-dir write grant.
- PFactory, the contract author. Any plan it emits with
  `deployment.satisfied_gates` changes meaning.
- `scripts/gen_autonomy_matrix.py` and `docs/docs/compliance/autonomy-matrix.md`
  / `docs/static/compliance/autonomy-matrix.json`: the matrix fixtures pass
  `satisfied_gates` (`gen_autonomy_matrix.py:67`, `:174`), and the published
  claim.
- Operators of projects whose contracts declare `system_gates`. Today those
  tasks may auto-merge, and after the fix they will hold until approved.

## Constraints

- The overlay may only tighten (RFC-0013). No change may make any
  disposition looser than it is today.
- Fail closed: if approval evidence is unreadable or missing, the gate stays
  outstanding. The merge must not raise or abort the endgame
  (`pr_endgame.py:501-505` already holds on read errors).
- Contracts without a `deployment` block, and AIFactory's own create-and-run
  tasks with no contract, decide exactly as before.
- `load_task_contract` must keep feeding TFactory the declared acceptance
  criteria. Changes to how the merge path trusts the file must not break
  that handoff.
- The signing key and envelope format are shared with PFactory and must not
  change in a way that rejects plans already signed and in flight.
- Regenerate the autonomy matrix from code. Do not hand-edit it (#1962).

## Open questions

1. Should `deployment.satisfied_gates` in the contract be ignored outright,
   or still honoured for gates that are not human-only (e.g. a `security-scan`
   that PFactory ran before signing)? The issue proposes "never from the
   contract itself".
2. What is the trusted source of a `human-approval`: a GitHub PR review
   approval by someone other than the bot and author, an AIFactory UI/API
   action written to `task_metadata.json` by the web server, or both? Today
   nothing writes `satisfiedSystemGates`, so this decides whether the fix ships
   with a way to clear the gate or just holds every such task.
3. The coding agent can rewrite `context/task_contract.json` itself
   (`client.py:756-757`). That lets it drop `system_gates`,
   `production_classification` or `risk_class`, a wider hole than this issue.
   Should #1663 cover it (e.g. re-verify the signature at merge time, or move
   the contract out of the agent's writable tree), or should it be filed
   separately?
4. Should `task_metadata.json` stay a source of gate evidence at all, given
   the agent can write it? If it stays, who is allowed to write the key, and
   how does the merge path tell that writer apart from the agent?
