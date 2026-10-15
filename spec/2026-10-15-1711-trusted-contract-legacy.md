---
status: draft
issue: 1711
intent: intent/2026-10-15-1711-trusted-contract-legacy.md
---

# Spec: an explicit legacy verdict, and a kubejob check that the pod acts on it

## Design

**D1. The server always sends a value.** `spawn_env`
(`apps/web-server/server/services/trusted_contract.py:59-64`) returns
`{ENV: "legacy"}` for the legacy state instead of `{}`. Both spawn sites
(`agent_service.py:1238-1240` in-pod, `build_backend.py:1267-1269` kubejob)
already merge the dict, so neither changes. `core/contract_trust.py` gains
`LEGACY = "legacy"`, which the server imports next to `ENV`.

**D2. The pod reads `legacy`.** In `trusted_contract`
(`apps/backend/core/contract_trust.py:65-89`):

| contract file | ENV | result |
|---|---|---|
| missing / not a dict | unset, `hold` or `legacy` | `legacy`, `None`, "no contract" (was: `legacy` gave a digest-mismatch hold) |
| a dict | `legacy` | `hold`, the contract, "server saw no signed contract" (new label) |
| every other row | | unchanged |

Why hold a contract under `legacy`: the only writer of
`context/task_contract.json` is the signed ingest (`trusted_plan.py:862`), and
its contract carries an `approval` envelope, so the server would see a trace and
say `hold`, never `legacy`. A contract file the pod sees under `legacy` is one
the server did not vouch for. This closes the #1673 residual gap. A pod with no
server (ENV unset) keeps today's trace-based fallback.

**D3. Prompt readers stay quiet under `legacy`.** `_load_contract`
(`apps/backend/prompts_pkg/prompts.py:586-604`) today withholds the
`implementation_plan.json` fallback whenever ENV is present, with a warning.
Under `legacy` it returns `None` without the warning: every ordinary server
build would otherwise log it three times per prompt. The fallback only fed
contract fields (`epic_context`, `deployment`) that a legacy plan does not
carry, so withholding it changes no prompt for a real build.

**D4. Kubejob integration check (#1710).** One parametrized test in
`tests/test_trusted_contract_isolation_stamp.py` runs the real kubejob
dispatch (`_kubejob_dispatch`) with the server verdict forced per case through
`resolve_contract`, reads `AIFACTORY_TRUSTED_CONTRACT` from the created Job
manifest, sets it in the environment, and calls the pod's build path
`cli.build_commands._resolve_build_contract` on a pod spec dir holding a
migration contract:
verified → returns the contract; hold → exits 1; legacy with the file → exits
1; legacy with no file → returns `None`.

## Alternatives rejected

- **`legacy:<digest>` so the pod can act on a legacy contract the server saw.**
  The server's legacy loader (`load_task_contract`) and the pod's
  (`load_contract`) read different files, and no legitimate unsigned contract
  file exists to protect. More code for no case.
- **Treat `legacy` exactly like unset.** Sends a value but closes no gap.
- **Keep the warning in prompt readers under `legacy`.** Log noise on every build.
- **Real signing in the integration test.** The verified server decision is
  already covered (`test_spawn_env_verified_is_digest`,
  `test_cross_side_digest_matches_pod_verdict`); the integration test checks the
  transport and the pod's action.

## Risks

- **Image skew.** If `AIFACTORY_BUILD_IMAGE` pins an older pod image, an old pod
  reads `legacy` with no contract as a digest-mismatch hold: a normal build
  continues without contract features and logs a hold warning. Fails closed.
- **An unsigned contract file under a server is now held.** A migration in that
  state stops (exit 1). Only hand-placed files are affected.
- The `spec_runner` chain still carries no verdict (unchanged, #1673 Risks).

## Verification

- `tests/test_contract_trust.py`: legacy with no file is silent legacy; legacy
  with a file holds with the new label.
- `apps/web-server/tests/test_trusted_contract_merge.py`: legacy spawn env is
  `{ENV: "legacy"}`.
- `tests/test_trusted_contract_isolation_stamp.py`: kubejob and in-pod legacy
  spawns carry `legacy`; a stray server value is still overwritten; the new
  integration test (D4).
- `apps/backend/prompts_pkg/test_house_standards_prompt.py`: legacy withholds
  the plan fallback without the warning.
- The legacy rows of the integration test fail on the old code.
- `ruff check`, `ruff format --check`, mypy on touched backend files.
