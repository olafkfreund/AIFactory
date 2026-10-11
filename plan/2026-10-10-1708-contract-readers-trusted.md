---
status: draft
issue: 1708
spec: spec/2026-10-10-1708-contract-readers-trusted.md
---

# Plan: the remaining task-contract readers act only on a trusted contract

You can implement this plan without opening the intent or the spec. Line
numbers are for HEAD `c1664046` unless they say "dev". After step 0 they hold
for every file this plan edits except `core/auth.py`, which has its own dev
numbers below.

## Approved decisions

- **D0 (Q5): one PR to `dev` with all three production changes.**
  - Rebase onto `origin/dev` before any code edit.
  - Correction to the spec: the branch is **5** commits behind, not 1 (as of
    2026-10-11). They are `1deb7faa` #1674, `ed266886` #1689, `bf4fd672` #1688,
    `0bf19434` #1690 and `4470e4c7` #1728.
  - Those commits move lines only in `core/child_env.py`,
    `completion_orchestration.py`, `CHANGELOG.md`, `scripts/bump-version*`,
    `core/auth.py`, `tests/test_agent_env_scrub.py` and
    `tests/test_child_process_env.py`. In `auth.py` the change is #1674's +8
    lines. The two test files are cited below with dev numbers.
  - `prompts.py`, `tfactory_client.py`, `contract_trust.py`, `client.py`,
    `simple_client.py` and every other test file below are identical on HEAD
    and dev.
- **D1: one trusted reader.**
  - Replace the body of the existing `_load_contract` in `prompts_pkg/prompts.py`.
  - Make `get_house_standards_context` and `get_constitution_context` call it.
    `get_deployment_context` already does.
  - Remove the duplicated read loops. Add no new function name.
- **D2 (Q1): the reader follows `contract_trust.trusted_contract(spec_dir)`.**
  - `hold`: log one constant warning,
    `"[trusted-contract] contract prompt blocks withheld: %s"` with
    `contract_trust.reason_label(why)`, and return None.
  - A non-None contract (verified, or legacy with `task_contract.json`): return it.
  - legacy with a None contract, when `contract_trust.ENV in os.environ` or
    `contract_trust.has_trusted_trace(spec_dir)`: log the constant warning
    `"[trusted-contract] implementation_plan.json is not verified"` and return
    None. This closes two holes:
    - hole (a): the env says hold, `task_contract.json` is deleted, and the plan
      is forged.
    - hole (b): the approval exists only in the plan.
  - Otherwise, the true-legacy fallback: read `implementation_plan.json` and
    return it only if it is a dict. Return None on `OSError`/`ValueError`.
  - It never raises.
- **D3 (Q2): withhold on hold, never fail the build.**
  - On hold, the house standards, constitution and deployment getters each
    return `""`.
  - Logs carry only constant strings and `reason_label()`. Never log the digest,
    the contract, `why` or the env value (CodeQL).
- **D4: imports in `prompts.py`.**
  - Add `import logging`, `from typing import Any`, `from core import
    contract_trust` (a module import, no alias) and
    `logger = logging.getLogger(__name__)`.
  - This is the first `core.*` import in `prompts_pkg`. There is no import
    cycle: `contract_trust` imports only the stdlib at module level, and
    `core/__init__` is lazy.
- **D5: the return type becomes `dict[str, Any] | None`.**
  - This replaces the bare `dict | None` and lowers the strict mypy count for
    `prompts.py` from 5 to 4.
  - The verified/legacy branch tests `isinstance(contract, dict)`, not the
    spec's `contract is not None`. Same behaviour (`trusted_contract` returns a
    dict or None), but the ratchet's mypy runs with `--ignore-missing-imports`
    from the repo root, so `core.contract_trust` resolves to `Any` and
    `return contract` is a new `no-any-return`. The `isinstance` narrows it.
    With `is not None` the count stays at 5 (dry run, 2026-10-11).
  - The callers keep their `isinstance` checks.
  - Docstrings that say "signed Task Contract ... falling back" become
    "verified contract, or the legacy fallback".
- **D6: an accepted legacy edge.** A `task_contract.json` that parses to a
  non-dict used to give `""` for house standards and constitution, because the
  old loop broke on the first parse. They now take the `implementation_plan.json`
  fallback, as deployment already did.
- **D7 (Q3a): TFactory `contract` becomes required.**
  - In `build_ingest_payload` and `maybe_auto_handoff_tfactory` it becomes
    `*, contract: dict[str, Any] | None`, keyword-only with no default.
  - An explicit `None` still means "legacy": the server decided, and the call
    goes to `load_task_contract`. `{}` means held.
  - `load_task_contract` is unchanged, and so is every production caller.
  - Leaving `contract` out now raises `TypeError`.
- **D8 (Q4a): deny the verdict in the agent env.**
  - Add `"AIFACTORY_TRUSTED_CONTRACT"` to `_AGENT_ENV_DENY_EXACT` in
    `core/auth.py`, after #1674's entries, with a comment naming #1708.
  - `get_agent_env_blanks` then blanks it for both SDK clients, and an empty
    value reads as hold.
  - Do not edit `core/client.py`, `core/simple_client.py`, `core/child_env.py`,
    `trusted_plan.py` or web-server `services/trusted_contract.py`.
  - `child_env` already strips the name through `_STRIP_VARS`. The verdict is
    added to `spawn_env` after scrubbing (dev `agent_service.py:1239` and
    `build_backend.py:1269`), so `run.py` keeps the real value.
- **D9: regenerate the autonomy matrix in the prompts commit and in the
  `auth.py` commit.**
  - It cites `core.auth:321` on dev (`autonomy-matrix.json:695,703`,
    `autonomy-matrix.md:154-155`); the new entry moves it to `:323`.
  - The prompts commit also changes it: `prompts_pkg/__init__.py` joins two
    gates' `init_reach` (step 1i).
  - Never hand-edit it. `gen_autonomy_matrix.py --check` must pass.
- **D10: prompt tests.**
  - Add an autouse `delenv(contract_trust.ENV)` fixture.
  - House and deployment get five cases: hold with caplog, digest mismatch,
    verified, hole (a) and hole (b).
  - Constitution gets one hold case and one verified case.
  - Every new test is typed `-> None`.
  - The existing fallback tests stay unchanged.
- **D11: TFactory tests.**
  - Add `contract=None` at exactly 25 call sites.
  - Add a `TypeError` test for a missing `contract`.
  - `test_trusted_contract_merge.py` and the patch-only tests need no edit.
- **D12: the env test.** Add `"AIFACTORY_TRUSTED_CONTRACT": "x"` to `SECRETS` in
  `tests/test_agent_env_scrub.py`.
- **D13: CHANGELOG.** Add one entry under `[Unreleased]` → `### Security`. On a
  conflict, keep both entries.
- **D14: mutation checks.** Apply each mutation by edit and restore it by edit.
  Never use `git stash`, `checkout` or `restore`.
- **D15: process.**
  - Commit scopes contain no `#`.
  - The PR targets `dev`.
  - More than 3 steps edit files and about 12 files change, so the `coder` agent
    does the work.

### Spec corrections, recorded here

1. **The QA site was mislabelled.** The spec's "QA fixer :967-975" is wrong.
   Lines `:901-904` (QUICK_MODE early return) and `:967-975` (full mode) are both
   in `get_qa_reviewer_prompt` (def `:863`). `get_qa_fixer_prompt` (def `:1003`)
   injects none of these blocks. The coverage claim still holds, because every
   injection site goes through the three getters.
2. **One docstring was missing.** `get_deployment_context`'s docstring at `:705`
   also says "Reads the signed Task Contract". Reword it with the others.
3. **Dev line shifts in files that are cited but not edited:**
   - `child_env`: `_STRIP_VARS` is at `:20-24` (the entry is at `:23`), the
     filter at `:53`, and `extra` is applied at `:58-59`.
   - `completion_orchestration`: the calls are at `:243-245` and `:293-297`.

### Additions beyond the spec (deviations, kept deliberately)

- **Two more deployment tests:** `test_non_dict_plan_fallback_returns_empty` and
  `test_malformed_plan_fallback_never_raises`.
  - `get_deployment_context` (`:712`) has no `isinstance` check after the call.
  - These are the only tests that catch mutations M4 and M5.
- **A second `TypeError` test, for `maybe_auto_handoff_tfactory`.** The single
  test in the spec cannot see a default left on that function, because it passes
  `contract=contract` through at `:826`.

## Setup for every step

- **Python.** The worktree has no venv. Use
  `PY=/mnt/code/Source-home/GitHub/AIFactory/apps/backend/.venv/bin` and
  `WPY=/mnt/code/Source-home/GitHub/AIFactory/.venv/bin` for the web server.
  Before committing, put `$PY` on `PATH` so the pre-commit hook runs.
- **Where to run tests.**
  - Run `prompts_pkg/` and `pfactory/` tests from `apps/backend`. Run alone from
    the root, they fail with `ModuleNotFoundError: prompts_pkg`.
  - Run `tests/` from the repo root.
  - Web-server tests need `-o asyncio_mode=auto`.
- **GATES** (every commit):

  ```
  ruff format --check apps/backend apps/web-server scripts tests && ruff check apps/backend apps/web-server scripts tests
  $PY/python scripts/cq_ratchet.py --staged --ruff "$(command -v ruff)" --config standards/ruff.toml --paths 'apps/backend/*.py' 'apps/web-server/*.py' 'scripts/*.py'
  $PY/python scripts/cq_ratchet.py --staged --tool mypy --mypy "$(command -v mypy)" --config standards/mypy.ini --paths 'apps/backend/*.py' 'apps/web-server/*.py' 'scripts/*.py'
  $PY/python scripts/gen_autonomy_matrix.py --check
  ```

- **Known noise.** `tests/test_security.py` GitCommitValidator fails while files
  are staged. That is environmental; ignore it.
- **Env leakage.** Keep `AIFACTORY_TRUSTED_CONTRACT` unset in your shell. The
  autouse fixture covers only the three prompt test files, and
  `tests/test_solo_mode.py` is not covered.
- **The coder never uses** `git stash`, `checkout` or `restore`.

## Steps

0. **Branch: rebase, no file edits.**
   - Run `git fetch origin dev && git rebase origin/dev`. The branch's own
     commits touch only `intent/`, `spec/` and `plan/`, so no conflicts are
     expected.
   - Verify by:
     - `git rev-list --count HEAD..origin/dev` prints `0`.
     - `sed -n 158,159p apps/backend/core/auth.py` shows `"S3_ACCESS_KEY",` then `}`.
     - `cd apps/backend && $PY/python -c 'import prompts_pkg'` exits 0.
   - Re-measure the C1 baseline (see Tests).
   - Traps:
     - The pre-rebase matrix cites `core.auth:313` and dev cites `:321`. Never
       carry the old matrix forward; regenerate it.

1. **Prompts: one trusted reader (D1-D6, D9, D10).** Write the tests first, then the
   code. One commit:
   `fix(prompts): read the task contract only through trusted_contract`.

   1a. **`apps/backend/prompts_pkg/test_house_standards_prompt.py:1-9`, append after `:84`.**

   Add `import logging`, `import pytest` and `from core import contract_trust`.
   Then add the typed autouse fixture:

   ```python
   @pytest.fixture(autouse=True)
   def _no_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
       monkeypatch.delenv(contract_trust.ENV, raising=False)
   ```

   Add five tests, each `-> None`, typing `tmp_path: Path`,
   `monkeypatch: pytest.MonkeyPatch` and `caplog: pytest.LogCaptureFixture`:

   - `test_hold_withholds_block_and_logs_label`
     - Setup: write `_standards()` to `context/task_contract.json`, then
       `setenv(ENV, "hold")`.
     - Expect: under `caplog.at_level(logging.WARNING)` the result is `""`, and
       `"held by server" in caplog.text`.
   - `test_digest_mismatch_withholds_block`
     - Setup: the contract on disk, `setenv(ENV, "0" * 64)`.
     - Expect: `""`.
   - `test_verified_renders_same_block_as_legacy`
     - Setup: the contract on disk. With no env, take `legacy =` the result;
       `assert legacy`. Then `setenv(ENV, contract_trust.contract_digest(_standards()))`.
     - Expect: the result `== legacy`.
   - `test_hold_ignores_plan_fallback_hole_a`
     - Setup: no `task_contract.json`. Write `_standards()` to
       `implementation_plan.json` with no `approval`, and `setenv(ENV, "hold")`.
     - Expect: `""`, and `"implementation_plan.json is not verified" in caplog.text`.
   - `test_plan_approval_trace_ignores_fallback_hole_b`
     - Setup: no env and no `task_contract.json`. The plan is
       `{**_standards(), "approval": {"by": "pfactory"}}`.
     - Expect: `""`.

   Keep `test_falls_back_to_implementation_plan` (`:75`) unchanged.

   1b. **`apps/backend/prompts_pkg/test_deployment_prompt.py:1-9`, append after `:105`.**
   - The same imports, fixture and five cases, using `_deployment()`.
   - Two more tests:
     - `test_non_dict_plan_fallback_returns_empty`: the plan is `[1, 2]`, and
       the result is `""`.
     - `test_malformed_plan_fallback_never_raises`: the plan is `{not json`, and
       the result is `""`.
   - Keep `:88-90` unchanged.

   1c. **`tests/test_constitution_prompt.py:17-27`, append after `:178`.**
   - Add `import logging` and `import pytest`. Add `from core import
     contract_trust  # noqa: E402` after the `sys.path` insert, in the same
     style as `:27`.
   - Add the same fixture, then `test_hold_withholds_constitution` (caplog
     `"held by server"`) and `test_verified_renders_same_block_as_legacy`
     (`_constitution()`).
   - Keep `:145` unchanged.

   Red check:

   ```
   cd apps/backend && $PY/python -m pytest -p no:cacheprovider -q prompts_pkg/test_house_standards_prompt.py prompts_pkg/test_deployment_prompt.py ../../tests/test_constitution_prompt.py
   ```

   The hold, mismatch and hole (a)/(b) tests fail. The verified tests may
   already pass.

   1d. **`apps/backend/prompts_pkg/prompts.py:10-23`: imports.**
   - Add `import logging` and `from typing import Any` to the stdlib block
     (`:10-13`).
   - Add `from core import contract_trust` before `from .project_context`
     (`:15`).
   - Add `logger = logging.getLogger(__name__)` next to `PROMPTS_DIR` (`:23`).

   1e. **`apps/backend/prompts_pkg/prompts.py:596-612`: replace `_load_contract`.**

   ```python
   def _load_contract(spec_dir: Path) -> dict[str, Any] | None:
       """Return the verified contract, or the legacy fallback; None if held or untrusted (#1708)."""
       state, contract, why = contract_trust.trusted_contract(spec_dir)
       if state == "hold":
           logger.warning("[trusted-contract] contract prompt blocks withheld: %s", contract_trust.reason_label(why))
           return None
       if isinstance(contract, dict):  # verified, or legacy with task_contract.json
           return contract
       if contract_trust.ENV in os.environ or contract_trust.has_trusted_trace(spec_dir):
           logger.warning("[trusted-contract] implementation_plan.json is not verified")
           return None
       # ponytail: true-legacy fallback, drop once every task carries a server verdict
       try:
           data = json.loads((spec_dir / "implementation_plan.json").read_text())
       except (OSError, ValueError):
           return None
       return data if isinstance(data, dict) else None
   ```

   Let `ruff format` wrap the long lines. Keep the docstring as written: it is
   98 characters, and `ruff format` never wraps a docstring (E501 trap below).
   Keep `isinstance(contract, dict)` (D5).

   1f. **`prompts.py:364-385` (`get_house_standards_context`).**
   - Change the docstring at `:367-368` to "verified contract, or the legacy
     fallback".
   - Replace `:375-383` (`contract = None` and the loop) with
     `contract = _load_contract(spec_dir)`.
   - Keep the `isinstance` check at `:384-385`.

   1g. **`prompts.py:502-530` (`get_constitution_context`).** Make the same
   docstring change at `:505-506`. Replace `:520-528` and keep `:529-530`.

   1h. **`prompts.py:705-706` (`get_deployment_context`).** Reword the docstring
   to match (spec correction 2). Replace the two lines starting "Reads the
   signed Task Contract and surfaces" with exactly:

   ```
       Reads the verified contract, or the legacy fallback (``_load_contract``), and
       surfaces ``deployment`` so the coder / QA agents PLAN FOR HOW THE CHANGE
       SHIPS, not just what it does: the required
   ```

   The logic at `:712` stays as it is.

   1i. **Matrix.** Run `$PY/python scripts/gen_autonomy_matrix.py` and commit
   `docs/static/compliance/autonomy-matrix.json` and
   `docs/docs/compliance/autonomy-matrix.md` with this step. The new
   `prompts_pkg -> core.contract_trust` import adds
   `apps/backend/prompts_pkg/__init__.py` to the `init_reach` of
   `pr_review_service` and `pr_endgame` (counts 14 -> 15 and 20 -> 21), so
   `--check` fails without it (dry run, 2026-10-11).

   Verify by:
   - Re-running the red check: everything passes.
   - `cd apps/backend && $PY/python -c 'import prompts_pkg'`.
   - `$PY/python -m pytest -q prompts_pkg`.
   - From the root, `$PY/python -m pytest -q tests/test_constitution_prompt.py tests/test_solo_mode.py`.
   - `grep -n "logger\." apps/backend/prompts_pkg/prompts.py`: only the two
     constant-format calls appear.
   - GATES.

   Traps:
   - **CodeQL** (`py/clear-text-logging-sensitive-data`, `py/log-injection`).
     Log only constant strings and `reason_label(why)`. Never log `contract`,
     `why`, the digest or the env value. Do not bind a variable named
     `trusted*`, `*token*` or `*key*` next to the log call.
   - **Strict ratchet.** Both `prompts_pkg/test_*.py` files fall under the
     strict ratchet. Type every new function and fixture parameter. Leave the
     existing untyped tests alone.
   - **Root test path.** `contract_digest` imports `trusted_plan`. The root
     `tests/` file's `sys.path` block must already add `apps/backend`; confirm
     that it does.
   - **D6 edge.** A non-dict `task_contract.json` now takes the plan fallback.
     This is accepted; do not "fix" it.
   - **Matrix drift is certain here** (1i). Regenerate; never hand-edit.
   - **E501 in docstrings.** The strict ruff ratchet counts per rule code
     (`prompts.py` has 7 E501 on dev). A reworded docstring line over 100
     characters is a regression that `ruff format` will not fix. Use the exact
     1h text; check with `awk 'length>100' apps/backend/prompts_pkg/prompts.py`
     (7 lines, all pre-existing).
   - **Injection sites are read-only.** Do not edit `:195`, `:201-202`, `:255`,
     `:258`, `:261`, `:901-904` or `:967-975`. They only confirm that every
     injection goes through the three getters.

2. **TFactory: `contract` is required (D7, D11).** Write the tests first, then
   the code. One commit:
   `fix(tfactory): require an explicit contract verdict for the handoff payload`.

   2a. **Add `contract=None` at 25 call sites:**

   | File | Lines |
   |---|---|
   | `apps/backend/pfactory/test_tfactory_client_issue.py` | `:36`, `:45`, `:54`, `:69` |
   | `apps/backend/pfactory/test_tfactory_source_branch.py` | `:40`, `:58`, `:74`, `:88` |
   | `tests/test_tfactory_auto_handoff.py` | `:37`, `:51`, `:79`, `:99`, `:110`, `:171`, `:186`, `:201`, `:220` |
   | `tests/test_tfactory_handoff_gitinfo.py` | `:37`, `:62`, `:102`, `:145`, `:272` |
   | `tests/test_subtask_queue_agreement.py` | `:223` |
   | `apps/web-server/tests/test_tenancy.py` | `:204`, `:208` |

   Edit by content, not by line number, then run `ruff format apps tests`.
   Several calls in `tests/test_tfactory_auto_handoff.py` wrap onto three lines,
   so `contract=None` is no longer on the call line.

   Verify the count with:

   ```
   grep -rnE "build_ingest_payload\(|maybe_auto_handoff_tfactory\(" apps tests | grep -v "def " | wc -l
   ```

   It prints `33` after 2b (31 on dev: the 25 sites, `:826`, three production
   calls and two in `test_trusted_contract_merge.py`).

   2b. **Append to `apps/backend/pfactory/test_tfactory_client_issue.py`:**

   ```python
   _MISSING = r"required keyword-only argument: 'contract'"

   def test_build_ingest_payload_requires_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
       _stub_git(monkeypatch)  # if the default comes back, the body runs: keep it off real git
       spec_dir = _spec_dir(tmp_path, {})
       with pytest.raises(TypeError, match=_MISSING):
           tc.build_ingest_payload(spec_dir, "001")  # type: ignore[call-arg, unused-ignore]

   def test_maybe_auto_handoff_requires_contract(tmp_path: Path) -> None:
       # async def: the bad call raises before a coroutine is created
       with pytest.raises(TypeError, match=_MISSING):
           tc.maybe_auto_handoff_tfactory(tmp_path, "001")  # type: ignore[call-arg, unused-ignore]
   ```

   Use the file's existing helper names. If `_stub_git` or `_spec_dir` differ,
   adapt to them.

   Red check:

   ```
   cd apps/backend && $PY/python -m pytest -q -p no:cacheprovider pfactory/test_tfactory_client_issue.py
   ```

   Only the two new tests fail.

   2c. **`apps/backend/pfactory/tfactory_client.py:577` and `:802`.** Drop
   `= None` from `contract` in both signatures. Do not change `:88`
   (`load_task_contract`), `:626-627` (held/None logic) or `:826` (pass-through).

   Verify by:
   - `cd apps/backend && $PY/python -m pytest -q pfactory`.
   - From the root:
     `$PY/python -m pytest -q tests/test_tfactory_auto_handoff.py tests/test_tfactory_handoff_gitinfo.py tests/test_subtask_queue_agreement.py`.
   - `cd apps/web-server && $WPY/python -m pytest -q -o asyncio_mode=auto tests/test_tenancy.py tests/test_trusted_contract_merge.py tests/test_handoff_tfactory_route.py`.
   - `grep -rn "maybe_auto_handoff_tfactory(\|build_ingest_payload(" apps/web-server/server`
     shows `contract=` at every production call: `execution.py:933-937`, and
     `completion_orchestration.py:243-245` and `:293-297` (dev).
   - GATES.

   Traps:
   - **`type: ignore` codes.** Use exactly `[call-arg, unused-ignore]`. The
     ratchet's mypy (repo root, `--ignore-missing-imports`) sees
     `pfactory.tfactory_client` as `Any`, so a bare `[call-arg]` is reported as
     `unused-ignore` and the file regresses 0 -> 2. A mypy run that resolves
     the module needs `call-arg`. The pair is clean in both (dry run).
   - **`test_tenancy.py` is strict.** It must stay clean under `mypy --strict`.
   - **Expected warning.** M7 produces a "never awaited" RuntimeWarning.

3. **Auth: blank the verdict in the agent env (D8, D9, D12).** Write the test
   first, then the code and the matrix. One commit:
   `fix(auth): blank the trusted-contract verdict in the agent env`.

   3a. **`tests/test_agent_env_scrub.py`, before the closing `}` of `SECRETS` (dev `:56`).**

   ```python
   # verdict digest; blank reads as hold in the agent (#1708)
   "AIFACTORY_TRUSTED_CONTRACT": "x",
   ```

   Red check: `$PY/python -m pytest -q tests/test_agent_env_scrub.py` fails.

   3b. **`apps/backend/core/auth.py`, after `"S3_ACCESS_KEY",` (dev `:158`).**

   ```python
   # #1708: the agent must read the verdict as hold, never the real digest.
   "AIFACTORY_TRUSTED_CONTRACT",
   ```

   `get_agent_env_blanks` (dev `:175-193`) needs no change.

   3c. **Matrix.** Run `$PY/python scripts/gen_autonomy_matrix.py`. Then commit
   `docs/static/compliance/autonomy-matrix.json` and
   `docs/docs/compliance/autonomy-matrix.md` with this step.

   Verify by:
   - `$PY/python -m pytest -q tests/test_agent_env_scrub.py tests/test_no_unscrubbed_spawn.py tests/test_child_process_env.py`.
     `test_extra_trusted_contract_survives_strip` (dev
     `test_child_process_env.py:303-305`) proves that `run.py` still receives
     the verdict through `extra`.
   - `grep -n AIFACTORY_TRUSTED_CONTRACT apps/backend/core/child_env.py` still
     shows `:23`.
   - GATES, including `gen_autonomy_matrix.py --check`.

   Traps:
   - **Hands off five files.** Do not edit `core/client.py`,
     `core/simple_client.py`, `core/child_env.py`, `trusted_plan.py` or
     `services/trusted_contract.py`.
   - **Leave the spawn order alone.** `spawn_env` adds the verdict after
     scrubbing; do not move that.
   - **Never hand-edit the matrix.**
   - **The pattern does not cover this name.** `_AGENT_ENV_DENY_PATTERN`
     (SECRET, PASSWORD, PRIVATE_KEY, CREDENTIAL, `(^|_)KMS`, PASSPHRASE,
     TRUSTED_PLAN_KEY) does not match the name. The exact entry is required.

4. **Mutation checks, CHANGELOG and PR (D13, D14).** One commit:
   `docs(changelog): note the trusted contract readers`.

   4a. **Mutation checks.** Apply each mutation by edit, run its command, revert
   it by edit, and re-run until green. Commands:

   - **P** (prompts): `cd apps/backend && $PY/python -m pytest -p no:cacheprovider -q prompts_pkg/test_house_standards_prompt.py prompts_pkg/test_deployment_prompt.py ../../tests/test_constitution_prompt.py`
   - **T** (TFactory): `cd apps/backend && $PY/python -m pytest -q -p no:cacheprovider pfactory/test_tfactory_client_issue.py`
   - **E** (env): `$PY/python -m pytest -q -p no:cacheprovider tests/test_agent_env_scrub.py`

   | # | Edit | Cmd | Must fail |
   |---|---|---|---|
   | M1 | delete the `if state == "hold": ... return None` block | P | 5: hold and mismatch in house and deployment, plus constitution hold |
   | M2a | guard becomes only `has_trusted_trace(spec_dir)` | P | 2: hole (a) in house and deployment |
   | M2b | guard becomes only `ENV in os.environ` | P | 2: hole (b) in house and deployment |
   | M3 | `state == "hold"` becomes `state != "legacy"` | P | 3: the verified tests |
   | M4 | fallback returns `data` without the `isinstance` check | P | 1: `test_non_dict_plan_fallback_returns_empty` |
   | M5 | `except (OSError, ValueError)` becomes `except OSError` | P | 1: `test_malformed_plan_fallback_never_raises` |
   | M6 | restore `= None` at `tfactory_client.py:577` | T | 1: `test_build_ingest_payload_requires_contract` |
   | M7 | restore `= None` at `tfactory_client.py:802` | T | 1: `test_maybe_auto_handoff_requires_contract` |
   | M8 | delete the `auth.py` deny entry | E | 2: `test_all_secrets_are_blanked`, `test_simple_client_env_is_scrubbed` |

   Afterwards, `git status --short` shows no source change outside this step's
   files.

   4b. **`CHANGELOG.md:1-3` (dev).** Add one #1708 bullet under `[Unreleased]` →
   `### Security`. It covers the three trusted prompt readers, the required
   TFactory `contract` and the verdict blanked in the agent env.

   4c. **Update this plan** in the same commit for any deviation found while
   implementing.

   Verify by: everything in Tests, then GATES.

   Traps:
   - **CHANGELOG conflicts.** `CHANGELOG.md` is shared with #1631, #1632,
     #1692, #1633 and #1620. On a conflict, keep both entries.

   4d. **PR to `dev`.**
   - Link the intent, spec and plan, and say which steps the coder did.
   - Name the spec corrections: the 4-commit drift, `get_qa_reviewer_prompt`
     `:901-904`/`:967-975`, the docstring at `:705`, and the dev line shifts.
   - Name the additions beyond the spec.
   - End with the attribution footer.
   - Draft these follow-up issues as text only (do not file them):
     1. Remove the true-legacy `implementation_plan.json` fallback in
        `_load_contract` once every launch sets a server verdict. This is the
        `ponytail:` ceiling.
     2. The web server's legacy `load_task_contract` call at
        `services/trusted_contract.py:90` still reads the agent-writable file
        when there is no verdict. Decide whether legacy handoff should end.

## Tests

Measure the baselines again after step 0. The numbers below were measured on
`origin/dev` `4470e4c7` and on a dry run of every step in a scratch worktree
(2026-10-11): all GATES passed, and M1-M8 failed exactly as listed in 4a.

| Run | Before | After |
|---|---|---|
| C1 backend suite | 194 passed (dev `4470e4c7`) | 210 passed (baseline + 16) |
| C2 new and edited files only | 36 | 52 (11 + 17 + 13 + 6 + 5) |
| C3 web server | 56 | 56 |

C1, the backend suite, from the repo root:

```
$PY/python -m pytest -p no:cacheprovider -q apps/backend/prompts_pkg apps/backend/pfactory tests/test_constitution_prompt.py tests/test_solo_mode.py tests/test_tfactory_auto_handoff.py tests/test_tfactory_handoff_gitinfo.py tests/test_subtask_queue_agreement.py tests/test_agent_env_scrub.py tests/test_child_process_env.py tests/test_no_unscrubbed_spawn.py tests/test_contract_trust.py tests/test_deploy_scaffold.py tests/test_migration_mapper.py tests/test_trusted_contract_isolation_stamp.py
```

C2, the new and edited files only:

```
$PY/python -m pytest -q -p no:cacheprovider apps/backend/prompts_pkg/test_house_standards_prompt.py apps/backend/prompts_pkg/test_deployment_prompt.py tests/test_constitution_prompt.py apps/backend/pfactory/test_tfactory_client_issue.py tests/test_agent_env_scrub.py
```

C3, the web server:

```
cd apps/web-server && $WPY/python -m pytest -p no:cacheprovider -q -o asyncio_mode=auto tests/test_tenancy.py tests/test_trusted_contract_merge.py tests/test_handoff_tfactory_route.py
```

Other checks, each expected to pass:

- **Import cycle:** `cd apps/backend && $PY/python -c "import prompts_pkg"`
  exits 0.
- **Lint and ratchets:** GATES. The mypy count for `prompts.py` drops 5 -> 4.
- **Autonomy matrix:** `gen_autonomy_matrix.py --check` passes.
- **Mutations:** M1-M8 each fail exactly as listed in 4a.

## Rollback

1. **Before merge.** Drop the branch, or `git revert` the step commits newest
   first. Revert step 1's and step 3's commits whole, so the matrix goes back
   with the code that moved it. Then run `gen_autonomy_matrix.py`, then
   `--check`.
2. **After merge to dev.** PRs land on dev as squash commits (one parent), so
   revert in one PR with `git revert <squash-sha>` (no `-m`),
   then:
   - regenerate the matrix if dev has moved, and run `--check`;
   - add a CHANGELOG revert note;
   - re-run C1 and expect the pre-change baseline.
3. **Partial rollback:**
   - Reverting the reader alone restores the unverified reads. No data to
     migrate.
   - Reverting the TFactory default alone is safe, because every production
     caller passes `contract=`.
   - Reverting the auth blank alone re-exposes the digest to the agent shell.
     `child_env` still strips it from child processes.
4. **Runtime effect:** none on stored state. The verdict env var is set per run,
   and prompts are rebuilt every run.

## Merge order against in-flight work

- **No code overlap.** No in-flight branch touches `prompts.py`,
  `tfactory_client.py`, `core/auth.py`, `tests/test_agent_env_scrub.py` or the
  #1708 test files. #1692's plan forbids edits to `child_env.py` and `auth.py`.
- **The autonomy matrix is shared** with #1627, #1620, #1692, #1624 and #1717.
  Whichever merges second rebases and regenerates the matrix (generate, then
  `--check`). Never hand-merge it.
- **Order:**
  1. Let #1631, #1632, #1692 and #1633 land first.
  2. #1708 can land before or after #1627 and #1620.
  3. #1708 can land ahead of #1720, #1719, #1717 and #1624, which then
     regenerate the matrix.
