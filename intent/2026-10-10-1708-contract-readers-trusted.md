---
status: approved
issue: 1708
author: olafkfreund
---

# Intent: the remaining task-contract readers act only on a trusted contract

Follow-up to #1673 (PR #1712, c59a0ea6). That change made deploy_scaffold and
the migration build act only on the verdict from
`core.contract_trust.trusted_contract()`. The issue is still open on dev
(c59a0ea6): no commit or PR fixes it.

## Problem

The agent can write `context/task_contract.json` and `implementation_plan.json`
in its spec dir. Three groups of code still trust those files:

1. **Prompt readers (live).** `prompts_pkg/prompts.py` reads both files
   directly from disk at :376 (`get_house_standards_context`), :521
   (`get_constitution_context`) and :603 (`_load_contract`, used by
   `get_deployment_context`). The results go into the solo and coding
   prompts (`get_solo_prompt`, `get_coding_prompt`). With a `hold` verdict, an edited contract's house standards,
   HARD constitution rules and deployment scans/gates still reach the prompt
   as if they were signed. A probe at c59a0ea6 confirmed this. The
   `implementation_plan.json` fallback is never checked by `trusted_contract()`.
   When a block is stripped, nothing is logged.
2. **TFactory loader (latent).** `pfactory/tfactory_client.py:88`
   `load_task_contract` reads the same files with no trust check. Today
   `build_ingest_payload` only reaches it when `contract=None`, and every
   current caller passes a server-decided contract. The web server also calls
   it on purpose for the legacy verdict
   (`web-server/server/services/trusted_contract.py:90`).
3. **Env var in the agent (hygiene).** The agent's SDK environment
   (`core/client.py:614`, `core/auth.py` `get_agent_env_blanks`) does not blank
   `AIFACTORY_TRUSTED_CONTRACT`, so a nested `run.py` inherits the digest or
   `hold`. In the #1673 review the owner said this is not exploitable, and
   asked for it to be blanked here.

## Proposed outcome

- On `hold`, no prompt carries house standards, constitution or deployment
  text taken from the agent-writable files.
- On a verified contract, prompts show the same content as today.
- Legacy builds (no env var, no trusted trace) behave exactly as today.
- No backend path can send an unverified contract to TFactory by leaving out
  `contract`.
- `AIFACTORY_TRUSTED_CONTRACT` is blank in the agent's own environment.

## Affected users and systems

- Coder agents on trusted-plan (RFC-0002) builds. Owners who rely on
  contract governance: RFC-0012 house standards, RFC-0015 constitution and
  RFC-0013 deployment.
- `apps/backend/prompts_pkg/prompts.py`, `apps/backend/pfactory/tfactory_client.py`,
  and `apps/backend/core/auth.py` or `core/client.py`.
- Tests that pin today's direct-read behaviour:
  `prompts_pkg/test_house_standards_prompt.py`,
  `prompts_pkg/test_deployment_prompt.py`,
  `pfactory/test_tfactory_client_issue.py` and
  `pfactory/test_tfactory_source_branch.py`.

## Constraints

- Hold rules from #1673 hold: any exception, and an empty env value, both
  mean hold.
- Legacy builds must not change.
- The web server's legacy call to `load_task_contract` must keep working. The
  server process never has the env var set.
- Logs carry constant labels only (`reason_label()`) and never the digest, so
  CodeQL stays green.
- The pod never gets the signing key. `trusted_plan.py` is not touched.
- Do not touch `core/child_env.py`, because #1689 and #1690 edit it. Its
  `_STRIP_VARS` entry stays.
- Changes to `core/auth.py` must land after #1674 merges, because #1674 edits
  and freezes the same names.
- The #1673 tests and `scripts/gen_autonomy_matrix.py --check` must stay
  green. The PR targets dev.

## Open questions

1. **`implementation_plan.json` fallback.** Should the prompt readers drop it,
   or keep it only for the legacy verdict?
2. **Prompts on hold.** Should the prompts leave out house standards,
   constitution and deployment with a warning, or should the build fail?
3. **TFactory.** Option (a): make `contract` required in
   `build_ingest_payload` and leave `load_task_contract` as the server's legacy
   loader. Option (b): gate `load_task_contract` itself, which breaks the
   server's legacy path.
4. **Where the env blank lives.** Option (a): `_AGENT_ENV_DENY_EXACT` in
   `auth.py`. This covers every runner, but has to wait for #1674 and
   overrides its freeze. Option (b): an explicit blank at `client.py:614`,
   which covers Claude only and leaves the other runners to #1692.
5. **Scope and order.** Ship one PR after #1674? Or split it, with the prompt
   and TFactory readers now and the env blank after #1674?
