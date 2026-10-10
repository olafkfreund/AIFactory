---
status: draft
issue: 1673
author: olafkfreund
---

# Intent: deploy_scaffold and migration_mapper act on a task contract nobody verified

Follow-up to #1667 (intent answer 4). #1667 / PR #1683 made the merge gate
read the contract through a verified path. Two backend readers still do not.

## Problem

The coding agent can write anything under the spec directory
(`apps/backend/core/client.py:755-757`), and `context/task_contract.json` lives
there. No hook protects that file. The PFactory signature is checked once, at
ingest (`apps/web-server/server/routes/execution.py:1382` calling
`verify_trusted_plan`, `apps/backend/trusted_plan.py:423`), and never again in
the backend.

Two backend readers parse the contract as plain JSON and act on it:

1. `scaffold_deploy_for_spec()` (`apps/backend/agents/deploy_scaffold.py:79-93`)
   runs when the build completes (`apps/backend/agents/coder.py:1092`), after
   the agent has had the whole build to edit the file. The `deployment` block
   chooses which vendored template is written into the worktree
   (`.github/workflows/deploy.yml`, `infra/main.tf`, `.tfactory.yml`). A
   tampered contract can pick a different cloud or template, or add a deploy
   workflow to a build that never had a deployment block. The result looks
   contract-authorised. The real harm is limited: `_place()` never overwrites
   an existing file, and the agent can already write those paths directly.

2. `load_contract()` (`apps/backend/core/migration_mapper.py:30-43`), whose
   docstring says "the signed Task Contract", runs before the agent loop
   (`apps/backend/cli/build_commands.py:411-428`). `change_mode`,
   `environment` and `tfactory.equivalence.module_map` decide whether the build
   is an RFC-0010 migration, which language it targets, whether the oracle is
   mounted and what gets scaffolded. A contract edited in an earlier session is
   read as-is on resume or re-run. A normal build can be turned into a migration,
   or a real migration quietly downgraded to an in-place edit.

Both fail open and quietly: errors become `[]` / `None` or a `debug` log.

## Proposed outcome

- Neither reader acts on a contract whose integrity has not been confirmed by
  something outside the agent's reach.
- Each reader has a defined behaviour for a verified, held and legacy
  (no trusted trace) contract, in line with #1667's three states.
- A held or rejected contract is visible to the operator, not a silent degrade.
- Builds without a trusted-plan contract behave exactly as today.

## Affected users and systems

- Trusted-plan builds with `context/task_contract.json`: PaaS deploy scaffolding
  and RFC-0010 migration builds.
- `apps/backend/agents/deploy_scaffold.py`, `apps/backend/agents/coder.py`,
  `apps/backend/core/migration_mapper.py`, `apps/backend/cli/build_commands.py`.
- #1667's verified reader in `apps/web-server/server/services/trusted_contract.py`
  and `trusted_contract_store.py`, which the issue asks to reuse.
- kubejob, sandbox and local build hosts, which differ in isolation and database
  access.
- Not affected: the merge gate, which already uses the verified path.

## Constraints

- Treat the spec directory as hostile. A trust signal written there (including
  a server-written verdict file) is worthless; see #1672 for the same mistake
  with `reviewTier`.
- The signing key (`AIFACTORY_TRUSTED_PLAN_KEY_*`) must never reach the agent
  or the build pod. Open PR #1691 (#1680) removes it from the `run.py` environment.
- A valid signature alone does not stop a replayed or swapped contract. #1667's
  bar is signature, isolation stamp and a match with the stored record
  (`_record_verified()`, `trusted_contract.py:49-60`). Matching that bar, or
  saying plainly that the fix is weaker, is required.
- #1667's isolation rule holds: only kubejob with a database counts as isolated
  (`host_isolated()`, `trusted_contract.py:18-28`) until #1680 lands.
- The verified reader cannot just be imported: it is async, database-backed and
  in the web server, and kubejob pods have no database. Multi-replica (#1669,
  #1677) and pod restarts must not break whatever channel carries trust.
- Neither reader may crash a build with an unhandled error
  (`build_commands.py:428`). Whether a held contract stops a build is a
  deliberate choice (open question 5), not an exception. Legacy builds keep working.
- The contract schema and `_canonical()` form must not change in a way that
  breaks #1667's record comparison.
- Existing tests stay green or change deliberately: `tests/test_deploy_scaffold.py`,
  `tests/test_constitution_prompt.py`,
  `apps/backend/prompts_pkg/test_deployment_prompt.py`, `tests/test_migration_mapper.py`.
- PR #1691 edits `trusted_plan.py`, `pfactory/tfactory_client.py`,
  `core/auth.py` and `core/client.py`. Expect conflicts if this lands first.

## Open questions

1. Scope: only the two readers in the issue, or also the siblings
   (`apps/backend/prompts_pkg/prompts.py:376, :521, :596-610` and
   `load_task_contract` in `apps/backend/pfactory/tfactory_client.py:87`)? If
   only the two, do the siblings get a follow-up issue?
2. Ordering: wait for PR #1691 / #1680 to merge first?
3. Source of trust: the server hands the build a verified contract or verdict
   through a channel the agent cannot write, the backend re-verifies, or
   something else?
4. Strength: signature only, or the full #1667 bar?
5. Behaviour on hold, per reader. Deploy: skip silently, or skip with a log or
   status signal? Migration: block the build, run as a non-migration build, or
   stop for a human?
6. Non-isolated hosts (local, sandbox, no database): always hold like the merge
   gate, or keep legacy behaviour?
7. At completion, does the scaffold use the verified start-of-build contract or
   a fresh read checked against it? Is a contract change during a build ever
   legitimate?
8. Is the deploy side a real fix or defence in depth, given the agent can write
   those files anyway and review catches them? Does that change priority?
9. On resume or re-run, re-verify, or trust the earlier session's verdict?
10. Observability: build log, task status, or a hold reason like the merge gate's?
11. Tests: unit coverage of hold and legacy per reader, or also a kubejob
    integration check?
