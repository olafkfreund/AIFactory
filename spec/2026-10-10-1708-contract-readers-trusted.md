---
status: draft
issue: 1708
intent: intent/2026-10-10-1708-contract-readers-trusted.md
---

# Spec: the remaining task-contract readers act only on a trusted contract

## Design

This PR fixes the prompt readers and the TFactory default. The env-var
blank moves to a follow-up issue (Q5 below). Production code changes in
two files: `apps/backend/prompts_pkg/prompts.py` and
`apps/backend/pfactory/tfactory_client.py`.

### Change 1: one trusted reader in `prompts_pkg/prompts.py`

There are three copies of the same read loop:

- `get_house_standards_context` (:375-385)
- `get_constitution_context` (:520-530)
- `_load_contract` (:596-612), which `get_deployment_context` uses

Each copy reads `context/task_contract.json` and then
`implementation_plan.json` with no trust check. This change replaces the
body of `_load_contract` with a reader that follows the verdict, and
changes the other two functions to call it. No new function name is
added.

```python
import logging
from core import contract_trust  # module import, not a multi-name import

logger = logging.getLogger(__name__)

def _load_contract(spec_dir: Path) -> dict[str, Any] | None:
    """The contract a prompt may show (#1708): verified, legacy, else None."""
    state, contract, why = contract_trust.trusted_contract(spec_dir)
    if state == "hold":
        logger.warning("[trusted-contract] contract prompt blocks withheld: %s",
                       contract_trust.reason_label(why))
        return None
    if contract is not None:  # verified, or legacy with task_contract.json
        return contract
    # ponytail: implementation_plan.json is never hashed, so it is read only
    # on a true legacy build: no server verdict and no trusted trace.
    if contract_trust.ENV in os.environ or contract_trust.has_trusted_trace(spec_dir):
        return None
    try:
        data = json.loads((spec_dir / "implementation_plan.json").read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None
```

**Why the extra guard on the fallback.** `trusted_contract()` returns
`("legacy", None, "no contract")` when there is no `task_contract.json`
and the env var is either unset or `hold` (`core/contract_trust.py:76-78`).
That return comes before the function ever calls `has_trusted_trace()`.
Without the guard, two holes stay open:

- **(a)** On a build the server held, the agent deletes
  `context/task_contract.json` and writes an `implementation_plan.json`
  with its own `epic_context` and `deployment`. The verdict becomes
  legacy, and the fallback puts that text into the prompt.
- **(b)** A trusted plan whose `approval` exists only in
  `implementation_plan.json` also lands in legacy/None, so its unverified
  text gets into the prompt.

The guard matches the intent's definition of legacy: no env var and no
trusted trace. The existing fallback tests set no env var and write no
`approval`, so they stay green.

**Coverage.** All four prompts that build these blocks go through the
three getters, so all four are covered by this one reader:

- solo (:195-202)
- coding (:255-261)
- QA reviewer (:901-904)
- QA fixer (:967-975)

**Result for each verdict:**

| Verdict | What the prompt gets |
|---|---|
| verified | Blocks built from the contract object that was hashed. Same content as today. |
| legacy, `task_contract.json` present | Same dict as today. |
| legacy, no `task_contract.json`, no env, no trace | Same `implementation_plan.json` fallback as today. |
| hold, including an empty env value or an exception | All three blocks are `""`, and one warning is logged per block. |

**Other edits in the same file:**

- The docstrings at :367, :505 and :599 say "signed Task Contract …
  falling back". They become "verified contract, or the legacy fallback".
- The `isinstance` checks after the call stay as they are.

### Change 2: `contract` is required in `pfactory/tfactory_client.py`

- **What changes:** in `build_ingest_payload` (:577) and
  `maybe_auto_handoff_tfactory` (:802), `*, contract: dict[str, Any] | None = None`
  becomes `*, contract: dict[str, Any] | None`. It stays keyword-only, but
  has no default.
- **What stays:**
  - The logic at :626-627 already treats `None` as legacy and `{}` as
    held, so it does not change.
  - `load_task_contract` (:88) does not change, so the server's legacy
    call (`web-server/server/services/trusted_contract.py:90`) keeps working.
- **Production callers:** they all pass `handoff_contract()` already
  (`routes/execution.py:933`, `services/completion_orchestration.py:244`
  and `:294`), so none of them changes.
- **A caller that leaves out `contract`:** the call raises `TypeError`, and
  mypy flags it for typed code. It can no longer read the files silently.

### Change 3: tests

**Prompt tests.** These go in `prompts_pkg/test_house_standards_prompt.py`
and `prompts_pkg/test_deployment_prompt.py`. Both files are under the strict
ratchet, so every test is typed with `-> None`.

- Add an autouse fixture: `monkeypatch.delenv(contract_trust.ENV, raising=False)`.
- Add these cases:

| Case | Setup | Expect |
|---|---|---|
| hold | env `hold`, contract on disk | `""`, and `caplog` holds `held by server` |
| mismatch | env set to a wrong digest | `""` |
| verified | env set to `contract_digest(contract)` | the same block that legacy renders |
| hole (a) | env `hold`, no `task_contract.json`, standards in `implementation_plan.json` | `""` |
| hole (b) | no env, `implementation_plan.json` with `approval` plus standards | `""` |

- The existing legacy fallback tests stay unchanged
  (`test_house_standards_prompt.py:75`, `test_deployment_prompt.py:88`).
- `tests/test_constitution_prompt.py` gets the same fixture, plus one hold
  case and one verified case. It is outside the ratchet.

**TFactory caller tests.** These 25 calls add `contract=None`:

| File | Calls |
|---|---|
| `apps/backend/pfactory/test_tfactory_client_issue.py` | :36, :45, :54, :69 |
| `apps/backend/pfactory/test_tfactory_source_branch.py` | :40, :58, :74, :88 |
| `tests/test_tfactory_auto_handoff.py` | :37, :51, :79, :99, :110, :171, :186, :201, :220 |
| `tests/test_tfactory_handoff_gitinfo.py` | :37, :62, :102, :145, :272 |
| `tests/test_subtask_queue_agreement.py` | :223 |
| `apps/web-server/tests/test_tenancy.py` | :204, :208 |

- Add one test that calling `build_ingest_payload(spec_dir, "001")` without
  `contract` raises `TypeError`.
- These files need no edit:
  - `test_trusted_contract_merge.py` already passes `contract=`.
  - These tests patch the function rather than call it:
    `test_build_evidence_gate.py`, `test_gate_marker_copyback.py`,
    `test_completion_lands_via_merger.py`, `audit/test_task_action_audit.py`
    and `test_handoff_tfactory_route.py`.

**Changelog.** Add one entry under `[Unreleased]` in `CHANGELOG.md`.

**Files left alone:**

- `core/auth.py` and `core/client.py`
- `core/child_env.py`, which #1689 and #1690 edit
- `trusted_plan.py`
- `web-server/server/services/trusted_contract.py`

### Proposed answers to the intent's open questions

These are proposed defaults for the approver to accept or change.

**Q1. The `implementation_plan.json` fallback.**
- **Proposed:** keep it for legacy only. The fallback runs only when all
  of these are true: the verdict is `legacy`, the returned contract is
  `None`, the env var is absent, and `has_trusted_trace()` is false. On
  `verified`, use the contract the verdict returns. On `hold`, read nothing.
- **Why:** `trusted_contract()` hashes only `context/task_contract.json`
  (`core/migration_mapper.py:37`), so the fallback can never be verified.
  The two extra conditions close holes (a) and (b) above, because
  legacy/None is also returned when env is `hold`
  (`contract_trust.py:76-78`). Dropping the fallback entirely would change
  legacy builds and break the pinned tests at
  `test_house_standards_prompt.py:75` and `test_deployment_prompt.py:88`.

**Q2. Prompts on hold.**
- **Proposed:** leave out house standards, constitution and deployment,
  and log a warning that uses only `reason_label()`. Do not fail the build.
  The logic lives in the one `_load_contract` helper.
- **Why:** build start already has a policy for held contracts. A held
  migration exits (`cli/build_commands.py:188-192`). Any other held
  contract runs as a normal build with a warning (`:193-196`).
  `agents/deploy_scaffold.py:91-95` follows the same pattern. A hold can
  also come from a transient server lookup failure
  (`web-server/server/services/trusted_contract.py:84`).

**Q3. TFactory.**
- **Proposed:** option (a). Make `contract` a required keyword, with no
  default, on both `build_ingest_payload` (:577) and
  `maybe_auto_handoff_tfactory` (:802). An explicit `None` still means
  the server decided legacy. `load_task_contract` stays as it is.
- **Why:**
  - `maybe_auto_handoff_tfactory` passes its own default through at
    :826, so both functions need the default removed.
  - Every production caller already passes `handoff_contract()`. That
    function returns `None` only on legacy
    (`services/trusted_contract.py:52-56`).
  - Option (b) would break the server's own legacy load (:90).

**Q4. Where the env blank goes.**
- **Proposed:** option (a). Add `AIFACTORY_TRUSTED_CONTRACT` to
  `_AGENT_ENV_DENY_EXACT` in `core/auth.py`, after #1674's entries, and
  add it to `SECRETS` in `tests/test_agent_env_scrub.py`.
- **Why:**
  - `get_agent_env_blanks` feeds both SDK clients (`core/client.py:614`
    and `core/simple_client.py:89`). Option (b) would miss simple_client.
  - `child_env` already strips the name (`core/child_env.py:19`). It
    applies `extra` after filtering (:52-53), so the verdict that
    `spawn_env` sets still reaches `run.py`.
  - A blank value reads as hold.
  - This change shifts line numbers in auth.py, so the autonomy matrix
    has to be regenerated, as it was for #1674.

**Q5. Scope and order.**
- **Proposed:** split the work.
  - The prompt and TFactory readers ship now as the #1708 PR to dev.
  - The env blank (Q4) moves to a follow-up issue that waits on #1716
    (#1674).
  - If #1716 merges before this PR is coded, fold the blank into this PR
    instead. It is one line plus one test entry.
- **Why:** `auth.py` is locked until #1674 merges, because #1674 edits
  `_AGENT_ENV_DENY_EXACT` at :151-158. The owner said the blank is
  hygiene and is not exploitable. The prompt reader is the live hole, so
  it should not wait. The other runners stay with #1692.

## Alternatives rejected

- **Add a trust gate to each of the three read loops.** This triples the
  trust logic, and that duplication is what this change removes.
- **Drop the `implementation_plan.json` fallback.** This changes legacy
  builds and breaks the pinned tests.
- **Keep the fallback gated only on the verdict being legacy/None.** That
  leaves holes (a) and (b) open.
- **Fail the build on hold.** A transient lookup failure also returns
  hold, and build start already decides what happens to a held build.
- **Gate `load_task_contract`.** This breaks the server's legacy load.
- **Cache the verdict (`lru_cache`).** Each call costs one file read and
  one hash, three times per prompt. There is no cache until profiling
  shows a need.
- **Fold the env blank into this PR now.** `auth.py` belongs to #1674
  (see Q5).

## Risks

- **The env var leaks into tests.** If `AIFACTORY_TRUSTED_CONTRACT` is set
  in CI or in a developer's shell, every legacy prompt test turns into a
  hold and fails. The autouse `delenv` fixture prevents this.
- **Some legacy-looking builds lose their blocks.** This applies to a
  build with no verdict whose `requirements.json` has
  `provenance.trusted_plan: true`, or whose plan carries `approval`. It is
  intended: the intent says a trusted trace without a verdict holds.
- **A small legacy edge changes.** When `task_contract.json` parses to
  something that is not a dict, house standards and constitution used to
  return `""`. They now take the fallback, which is what deployment
  already did. The plan records this.
- **Repeated warnings.** A held build logs up to three warnings per
  prompt, one per block. This is acceptable noise.
- **Imports.** This is the first `core.*` import in `prompts_pkg`.
  `contract_trust` imports only the stdlib at module level, so no cycle is
  added. Check this with `python -c "import prompts_pkg"` from
  `apps/backend`, and with the prompt test run below.
- **CodeQL.** The log line is a constant format string plus a
  `reason_label()` value. No variable named `contract`, `digest`, `token`
  or `trusted*` is logged. This keeps `py/clear-text-logging-sensitive-data`
  and `py/log-injection` green, the same as deploy_scaffold.
- **mypy ratchet.**
  - The bare `dict | None` becomes `dict[str, Any] | None`, so the
    violation count goes down.
  - Web-server test edits must stay clean under `mypy --strict`.
- **CHANGELOG conflicts with other PRs.** Keep both entries.

## Verification

- **Unit tests:**
  - `pytest apps/backend/prompts_pkg apps/backend/pfactory tests/test_constitution_prompt.py tests/test_solo_mode.py tests/test_tfactory_auto_handoff.py tests/test_tfactory_handoff_gitinfo.py tests/test_subtask_queue_agreement.py tests/test_child_process_env.py tests/test_no_unscrubbed_spawn.py`,
    plus the #1673 tests (grep for `contract_trust` under the test directories).
  - `cd apps/web-server && pytest -o asyncio_mode=auto tests/test_tenancy.py tests/test_trusted_contract_merge.py tests/test_handoff_tfactory_route.py`
- **Mutation checks.** Apply each by editing, run the tests, then restore
  by re-editing. Never use git stash or checkout.

  | Mutation | Tests that must fail |
  |---|---|
  | Remove the `state == "hold"` branch | the hold tests |
  | Remove the `ENV in os.environ or has_trusted_trace` guard | the hole (a) and (b) tests |
  | Restore the `= None` default | the TypeError test |

- **Lint:** `ruff format --check` and `ruff check` on
  `apps/backend apps/web-server scripts tests`.
- **Strict ratchet:**
  `python scripts/cq_ratchet.py --staged --ruff "$(command -v ruff)" --config standards/ruff.toml --paths 'apps/backend/*.py' 'apps/web-server/*.py' 'scripts/*.py'`,
  plus the `--tool mypy --mypy "$(command -v mypy)" --config standards/mypy.ini`
  variant.
- **Autonomy matrix:** `python scripts/gen_autonomy_matrix.py --check`.
  The matrix cites neither changed file, so regenerate it only if
  `--check` reports drift.
- **Known environmental failure:** `tests/test_security.py`
  GitCommitValidator fails while files are staged. This is not caused by
  the change.
- **Commits and PR:** the commit scope may not contain `#` (for example,
  `fix(prompts): …`). The PR targets dev.

**Plan size.** The plan has about five steps that edit files, across
about 10 files, so it goes to the `coder` agent:

1. The `_load_contract` reader.
2. The prompt tests.
3. Make TFactory `contract` required, and add the TypeError test.
4. Add `contract=None` at the 25 call sites.
5. The CHANGELOG entry and the matrix `--check`.

**Follow-up issue** (waits on #1716 / #1674): add
`AIFACTORY_TRUSTED_CONTRACT` to `_AGENT_ENV_DENY_EXACT` in `core/auth.py`,
add it to `SECRETS` in `tests/test_agent_env_scrub.py`, then regenerate
the autonomy matrix.
