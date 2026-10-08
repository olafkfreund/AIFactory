---
status: approved
issue: 1667
author: olafkfreund
---

# Intent: the coding agent can rewrite the task contract the merge gate trusts

## Problem

The live merge decision trusts a file the coding agent is allowed to edit.

`merge_disposition` (`apps/web-server/server/services/pr_endgame.py:459`)
reads the deployment block through `load_task_contract`
(`apps/backend/pfactory/tfactory_client.py:87`) at `pr_endgame.py:499`, and the
path-risk floor reads it the same way at `pr_endgame.py:253`.
`load_task_contract` is a plain JSON read: it takes
`context/task_contract.json`, falls back to `implementation_plan.json`
(`tfactory_client.py:103-106`), and returns the first file that has
contract markers. It does not check a signature.

The PFactory HMAC is checked once, at ingest. The `/from-plan` route
(`apps/web-server/server/routes/execution.py:1433`) calls
`ingest_trusted_plan` (`apps/backend/trusted_plan.py:803`), which verifies the
signature over `_plan_core` (`trusted_plan.py:157`, the plan minus its
`approval` envelope). It then writes the plan verbatim, envelope included, to
`implementation_plan.json` (`:830`) and `context/task_contract.json` (`:845`).
After that, nothing checks the signature again.

The coding agent has `Write`/`Edit` on `{spec_path}/**`
(`apps/backend/core/client.py:755-757`), and the contract lives in that tree. So
during the build the agent can:

- remove `deployment.production_classification` or `risk_class`. This defeats the
  #1658 hold that shipped in 3.8.1.
- add to or rewrite `deployment.satisfied_gates` / `system_gates`. These are read
  at `pr_endgame.py:327-330` and are the subject of #1663.
- add `satisfiedSystemGates` to `task_metadata.json` (`pr_endgame.py:315`,
  `:337-340`).
- delete `context/task_contract.json`. An absent file is treated as a task with
  no contract (`_contract_unreadable`, `pr_endgame.py:442-456`, returns False).
  The loader then falls back to `implementation_plan.json`, which the executor
  has already rewritten without contract markers, so it returns `{}` and nothing
  is held.

These findings, verified in the code, make the problem wider than the issue text:

1. **The agent can read the signing key.** At ingest the web server verifies
   with `load_keyring_from_env()` (`trusted_plan.py:111`), so the key is the
   web server's `AIFACTORY_TRUSTED_PLAN_KEY_<AUTHORITY>[__<KID>]` environment
   variable. The agent environment scrub (`apps/backend/core/auth.py:152`,
   applied at `core/client.py:611`) blanks names that match
   `SECRET|PASSWORD|PRIVATE_KEY|CREDENTIAL|_KMS|PASSPHRASE`. `TRUSTED_PLAN_KEY`
   matches none of them. A check of `get_agent_env_blanks()` with both key forms
   set confirmed it blanks neither. A build that runs as a subprocess of the web
   server therefore inherits the key, and the agent could re-sign a contract it
   has edited. Checking the signature at merge time would not catch that edit
   until the key is out of the agent's reach. The kubejob path builds its env
   explicitly (`apps/backend/core/job_dispatch.py:~520-536`) and does not appear
   to pass the key on, but that is not yet confirmed.
2. **The only record that a task was ingested as trusted is in files the agent
   can write.** That record is the stored envelope, the approval provenance
   (`_record_approval_provenance`, `trusted_plan.py:931`, written to
   `requirements.json`) and the review state (`_mark_review_approved`, `:956`).
   All of them are under the spec directory. Today the merge path cannot tell
   "never had a contract" from "had one and the agent deleted it".
3. **The merge gate is not the only reader.** These also read the contract:
   - the TFactory handoff (`tfactory_client.py:621`). It sends the contract,
     with `execution.phase_models` and `provenance.github_issue` added after
     the read, so TFactory tests the acceptance criteria it declares. A
     tampered contract changes what TFactory verifies, and so the verdict the
     merge gate later trusts.
   - `deploy_scaffold.py:84`, which scaffolds deploy artifacts from
     `deployment`.
   - `core/migration_mapper.py:37`.
   - the prompt builders in `prompts_pkg/prompts.py:376`, `:521` and `:603`.
     These read the contract back into the agent's own context, so they are not
     a gate.

The guarantee #1658 gives, "production and high risk always hold for a
human", holds only while the agent leaves the contract alone.

## Proposed outcome

- The merge path (both `merge_disposition` and the path-risk floor) acts only on a
  contract it can prove is the one the upstream authority signed. "Prove" means
  the coding agent could not have produced it.
- On a task ingested as trusted, a missing, unsigned, re-signed or tampered
  contract holds the merge (`HOLD_BLOCKING_DISPOSITION`), the same way an
  unreadable contract does under #1658. The log line says why, without file
  content in it.
- Tasks that were never ingested as trusted (create-and-run and label-driven
  builds, with no contract) decide exactly as they do today.
- The gate-satisfaction evidence the merge path reads (`satisfiedSystemGates`,
  `deployment.satisfied_gates`) cannot be added by the agent. #1663 can then
  build on a contract it can trust.
- A regression test shows that each tamper route listed above (strip, rewrite,
  delete, re-sign) ends in a hold.

## Affected users and systems

- `apps/web-server/server/services/pr_endgame.py`: `merge_disposition`,
  `_contract_unreadable`, `satisfied_system_gates` and the path-risk floor.
- `apps/backend/pfactory/tfactory_client.py`: `load_task_contract` and the
  TFactory handoff at `:621`.
- `apps/backend/trusted_plan.py`: the ingest, and what it records about a trusted
  task.
- `apps/backend/core/auth.py` / `core/client.py`: the agent's environment and
  permission grants.
- `apps/web-server/server/routes/execution.py` `/from-plan`.
- Operators running the PARR pipeline (PFactory → AIFactory → TFactory), and anyone
  who relies on the #1658 production/high-risk hold.
- TFactory, which receives the contract on the handoff.
- #1663, which is blocked on this.

## Constraints

- **Fail closed, consistent with #1658.** A contract the merge path cannot verify
  on a trusted task holds. It is never read as "no deployment". An exception
  while verifying is a hold, not a raise.
- **Never loosen an existing hold.** Every hold that fires today keeps firing:
  the #1658 unreadable-contract hold, production/high-risk, `system_gates`, the
  path-risk floor, handback/VAL/CI parity, and the import-failure hold.
  The change can only add holds.
- **Keep the TFactory handoff working.** Trusted builds still send the full
  contract, including the `tfactory` block and the added `phase_models` and
  `provenance`. Create-and-run builds still send none, so TFactory infers.
  `load_task_contract` keeps skipping unparseable files for the handoff
  (`pr_endgame.py:446-448`).
- Back-compat for tasks with no contract and no trusted ingest: their merge
  behaviour does not change.
- Key rotation keeps working: keyed kids and `AIFACTORY_TRUSTED_PLAN_RETIRED_KIDS`
  (`trusted_plan.py:22-41`).
- No contract or key content in log lines (py/log-injection, as in
  `pr_endgame.py:1303-1306`).
- Must land before #1663.

## Open questions

1. A task ingested as trusted, then rotated off its signing kid (retired or
   removed) before merge: hold, or accept on the strength of the ingest-time
   check?
2. Does keeping `AIFACTORY_TRUSTED_PLAN_KEY_*` out of the agent environment belong
   in this issue, or in a separate #363-family issue that this one depends on?
   Merge-time verification does not hold while the agent can read the key.
3. Are the other agent-writable merge signals in `task_metadata.json` in scope:
   `reviewTier`, `achievedVal`/`valFloor`, `ciParity` and the TFactory verdict
   (`merge_gate_signals`, `pr_endgame.py:388`)? Or does this issue cover only the
   contract and `satisfiedSystemGates`, with the rest filed separately?
4. Should the other contract readers (`deploy_scaffold.py:84`, the TFactory
   handoff) also refuse an unverified contract, or is only the merge path in
   scope?
5. In-flight tasks ingested before this ships have no tamper-proof record that
   they were trusted. Should they be treated as trusted (hold if the contract is
   missing or invalid) or as legacy (decide as today)?

### Answers (approved by olafkfreund, 2026-10-08)

1. Hold. A retired or removed kid fails verification at merge; the remedy is a re-sign or a human merge.
2. The scrub of `AIFACTORY_TRUSTED_PLAN_KEY_*` (and `AIFACTORY_TOKEN`) from the agent env ships ahead as a hotfix (#1668, release 3.8.2). The spec assumes it has landed.
3. Only the contract and `satisfiedSystemGates`. The other agent-writable merge signals in `task_metadata.json` are a separate issue.
4. Only the merge path enforces. The TFactory handoff sends the verified contract when there is one and does not block; `deploy_scaffold` is a follow-up.
5. In-flight tasks with an `approval` envelope or trusted provenance in `requirements.json` are treated as trusted (hold if the contract is missing or invalid); legacy only when there is no trace of a trusted ingest.
