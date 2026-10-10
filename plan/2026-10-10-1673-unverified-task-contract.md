---
status: approved
issue: 1673
spec: spec/2026-10-10-1673-unverified-task-contract.md
---

# Plan: deploy_scaffold and migration_mapper act only on a contract the server verified

Worktree `/mnt/code/Source-home/GitHub/AIFactory-1673`, branch
`fix/1673-unverified-task-contract`, base `origin/dev`, spec approved at
`671ca31c`. All paths are repo-relative.

## Approved decisions (self-contained summary)

**Problem in one line.** `apps/backend/agents/deploy_scaffold.py` and
`apps/backend/core/migration_mapper.py` read `context/task_contract.json` from
the spec dir and act on it. The agent can write that file, so today an
unverified contract can scaffold deploy files and switch a build into
migration mode. The server already knows whether a contract is trusted
(`resolve_contract()` in `apps/web-server/server/services/trusted_contract.py:71-94`,
the full #1667 bar). The build process cannot run that check: it has no DB and
must never hold the signing key. So the server passes its verdict to `run.py`
as one environment variable, and both readers go through one gate.

1. **Scope (D1).** Only `deploy_scaffold` and `migration_mapper` (via
   `cli/build_commands.py`) are gated. Not in scope, covered by one follow-up
   issue: the prompt readers (`apps/backend/prompts_pkg/prompts.py:376, :521, :603`)
   and `apps/backend/pfactory/tfactory_client.load_task_contract` (:88). TFactory
   already gets the verified `handoff_contract` from the server.
2. **Ordering (D2).** Do not wait for #1691/#1680. This change must not touch
   `apps/backend/trusted_plan.py`, `apps/backend/pfactory/tfactory_client.py`,
   `apps/backend/core/auth.py` or `apps/backend/core/client.py` (it only
   imports `trusted_plan._canonical`). The pod gets a digest, never the key.
   `host_isolated()` stays as is. Check:
   `git diff --name-only origin/dev...HEAD -- apps/backend/trusted_plan.py apps/backend/pfactory/tfactory_client.py apps/backend/core/auth.py apps/backend/core/client.py`
   prints nothing.
3. **Channel (D3).** Env var `AIFACTORY_TRUSTED_CONTRACT` (called ENV below):
   - `sha256(_canonical(contract))` hex digest when the server's state is `verified`;
   - the literal `hold` when the state is `hold`;
   - **unset** when the state is `legacy`. There is no explicit `legacy`
     value (follow-up).
4. **Strength (D4).** The digest is minted only from `resolve_contract()`
   returning `verified`. On kubejob, `stamp_spawn(..., "kubejob")` must run
   before the env is built, or `_record_verified` sees no `kubejob` stamp.
5. **Hold behaviour (D5).**
   - Deploy: scaffold skipped, `[trusted-contract]` warning logged.
   - Migration: a held contract is never acted on. If it claims
     `change_mode == "migration"`, the build stops before the agent loop with
     the hold reason via `sys.exit(1)` (same pattern as `build_commands.py:264`,
     `:520`). Otherwise the build runs as a normal build with `_contract = None`
     and a warning.
6. **Non-isolated hosts (D6).** Whatever `resolve_contract` says (it holds
   whenever there is a record, the lookup failed, or there is a trace with no
   record). With no server env at all (local CLI), the gate falls back to
   `has_trusted_trace`: hold if there is a trace, else legacy.
7. **Completion (D7).** At deploy-scaffold time (build completion) the file is
   read fresh, hashed, and compared with the env value, which is fixed for the
   process life. A contract never legitimately changes mid-build (a new one
   only arrives via ingest, which starts a new build), so a change is a
   mismatch and holds.
8. **Deploy gating is defence in depth (D8).** `_place()` never overwrites and
   the agent can write those paths itself; the gain is that a scaffold no
   longer looks contract-authorised. Kept because it costs one call.
9. **Resume (D9).** Re-verified on every spawn from the DB row. Nothing is
   persisted in the spec dir.
10. **Observability (D10).** A hold produces `logger.warning` plus a
    `print_status` line, both prefixed `[trusted-contract]`, with the reason:
    `held by server`, `digest mismatch`, or
    `trusted trace without a server verdict`. A held migration claim also
    exits non-zero (shows as a failed task). A task-status field is a follow-up.
11. **Tests (D11).** Unit tests only. A kubejob integration check is a follow-up.

### Design details

- **New module `apps/backend/core/contract_trust.py`** (~40 lines). It is the
  single gate for both readers and the resolver the D1 follow-up will reuse.
  New module because the server and both readers import it; not in
  `trusted_plan.py` because #1691 edits that file.
  - `ENV = "AIFACTORY_TRUSTED_CONTRACT"`.
  - `contract_digest(obj) -> str`: `hashlib.sha256(_canonical(obj).encode()).hexdigest()`,
    `_canonical` imported lazily from `trusted_plan`. Server and pod both call
    it, so the hashes cannot drift.
  - `_read_json(path)` and `has_trusted_trace(spec_dir)` are **moved** with
    bodies unchanged from `apps/web-server/server/services/trusted_contract.py:30-46`.
    The server module re-imports `has_trusted_trace` (it already imports
    backend modules). One predicate, so the server verdict and the pod
    fallback cannot disagree.
  - `trusted_contract(spec_dir) -> tuple[str, dict | None, str]` returning
    `(state, contract, reason)`, state in `verified | hold | legacy`. It reads
    the file **once** via `migration_mapper.load_contract` (returns None when
    missing, unreadable or not a dict), hashes that same object and returns it;
    nothing ever acts on a second read. It reads `os.environ` at call time
    (never at import). It does not log; callers log. Any exception returns
    `("hold", None, <reason>)`. On hold the contract is returned only so the
    migration caller can see what it claims; callers never act on it.
- **Truth table** (`present` = `ENV in os.environ`; empty string is present):

  | disk contract | ENV | result |
  |---|---|---|
  | missing / not a dict | unset or `hold` | `("legacy", None, ...)`, silent |
  | missing / not a dict | any other value | `("hold", None, "digest mismatch")` |
  | dict | `hold` | `("hold", contract, "held by server")` |
  | dict | `== contract_digest(contract)` | `("verified", contract, ...)` |
  | dict | any other value, incl. `""` | `("hold", contract, "digest mismatch")` |
  | dict | unset, `has_trusted_trace` True | `("hold", contract, "trusted trace without a server verdict")` |
  | dict | unset, no trace | `("legacy", contract, ...)`, exactly as today |
  | any exception | — | `("hold", None, <reason>)` |

  The silent missing-file rows exist so a DB outage (ENV=`hold` on every
  spawn) does not log a hold on every build that has no contract.
- **`migration_mapper.load_contract`** keeps signature and body; only the
  docstring changes from "the signed Task Contract" to "the on-disk contract,
  unverified; use `contract_trust.trusted_contract`".
- **Server `spawn_env(spec_dir) -> dict[str, str]`** (async, beside
  `handoff_contract` in `trusted_contract.py`), mirroring its mapping:
  `state, contract = resolve_contract(spec_dir, await lookup(spec_dir))`;
  `verified -> {ENV: contract_digest(contract)}`, `hold -> {ENV: "hold"}`,
  `legacy -> {}`. Carries a digest, never the key.
- **kubejob** (`build_backend.py`): the `stamp_spawn` import and await move
  above `extra_env = build_job_env(oauth_token)`; the stamp still precedes
  `create_namespaced_job` (#1667 "record never lags the build"). Then
  `extra_env.update(await spawn_env(...))`. `build_job_env` is a whitelist, so
  nothing else can inject ENV into the Job.
- **in-pod / sandbox** (`agent_service.py`): directly after
  `await stamp_spawn(...)` and before `create_subprocess_exec`, merge
  `await spawn_env(spec_dir)` into `env`. Stamp first, otherwise a `none`
  stamp could follow an earlier-minted digest.
- **Strip a stray value** in the one shared env builder used by both
  `run.py`-spawning server paths (`agent_service` and the spec_runner chain in
  `agent_spec_creation`, which `os.execv`s `run.py`), so an
  `AIFACTORY_TRUSTED_CONTRACT` in the server's own env cannot reach either
  path as a fake verdict. See DV1 for where.
- **Rejected** (do not reintroduce): HMAC re-verify in the backend (key would
  be in the pod; #1680 is removing it); a verdict file in the spec dir or
  worktree (agent-writable, #1672 class); importing `resolve_contract` into
  the backend (needs DB/web server); passing the whole contract via env or
  ConfigMap; a backend copy of `has_trusted_trace`; per-malformed-value table
  rows, a Verdict class, or a shared `log_hold` helper; raising inside
  `load_contract` (the caller's `except Exception` would swallow it); an
  explicit `ENV=legacy`; gating the prompt readers and `load_task_contract` now.
- **Accepted risks** (the release note must say migrations stop on in-pod and
  sandbox hosts until #1680):
  - sticky `none` stamp: re-ingesting the same contract does not clear it
    (`put()` only resets on changed text, `trusted_contract_store.py:58-59`);
  - DB outage or failed stamp: hold for every spawn;
  - the spec_runner chain gets no ENV and falls back to `has_trusted_trace`
    (fails closed);
  - the stamp reorder may leave a `kubejob` stamp with no Job (harmless);
  - "server said legacy" is indistinguishable from "no server"
    (follow-up: explicit legacy value);
  - the localized worktree spec copy (`build_commands.py:403-404`): a stale
    copy holds;
  - the digest is not a secret.

### Deviations from the spec (recorded here per the workflow)

- **DV1 — strip location.** The spec says `subprocess_env.py:24` `_STRIP_VARS`.
  Since #1691 that line is a `sys.path` insert and the strip list lives in
  `apps/backend/core/child_env.py:16`
  (`_STRIP_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY_FILE")`), applied by
  `core.child_env.child_env` (:41-47), which web-server
  `subprocess_env.child_env` (:43-53) and `make_subprocess_env` (:56-73) wrap.
  `child_env` applies `extra` **after** the strip (:49-50), so `spawn_env`
  merges still work. Add ENV there. Side effect: every `child_env` consumer
  (git, gh, in-process backend children) also drops it — harmless and
  desired. `core/child_env.py` is not in D2's forbidden list. Fallback only if
  a reviewer insists core stays untouched: pop ENV inside web-server
  `make_subprocess_env` before return.
- **DV2 — line numbers moved** (post-#1691 rebase): `build_backend.py`
  `build_job_env` call :1260, stamp import+await :1282-1285,
  `create_namespaced_job` :1288; `agent_service.py` stamp :1235, spawn :1237;
  spec_runner env built at `agent_spec_creation.py:208`, spawn :293, `os.execv`
  at `apps/backend/runners/spec_runner.py:396`;
  `tfactory_client.load_task_contract` :88; `trusted_plan._canonical` :162,
  contract written :854.
- **DV3 — `_resolve_build_contract` helper.** The hold branch in
  `build_commands` lives in a module-level
  `_resolve_build_contract(spec_dir) -> dict | None` so the new branches stay
  out of `handle_build_command`, which is already over the strict ratchet's
  branch/statement/complexity caps. Behaviour is still tested through
  `handle_build_command` (Tests C).
- **DV6 — backend path guard in the server module.** The spec has
  `trusted_contract.py` re-import `has_trusted_trace` from the backend at
  module level. Today a standalone `import server.services.trusted_contract`
  leaves `apps/backend` off `sys.path` (checked: `env -u PYTHONPATH PYTHONPATH=.
  python -c "import server.services.trusted_contract"` in `apps/web-server`),
  and the server only gets it on the path by import-order luck. So the module
  gets the same three-line `_BACKEND_DIR` guard as
  `apps/web-server/server/utils/subprocess_env.py:22-24` before the `core`
  import.
- **DV4 — test file placement.** The `spawn_env` mapping tests and the
  cross-side digest test go in `apps/web-server/tests/test_trusted_contract_merge.py`
  (it has the signing fixtures `_isolated_host`, `_sign`, `_spec`, `_rec`,
  `KEY`), not `tests/test_trusted_contract_isolation_stamp.py`, which keeps the
  kubejob/in-pod ordering tests. The stray-ENV strip test goes in
  `tests/test_child_process_env.py` beside `test_make_subprocess_env_keep_and_drop` (:87).
- **DV5 — separate pytest runs.** `tests/pytest.ini:7` sets
  `asyncio_mode = auto` only when every argument is under `tests/`. Root tests
  and `apps/web-server/tests` are always run as separate commands.

## Repo traps (every step)

- Shared setup for every command:
  `cd /mnt/code/Source-home/GitHub/AIFactory-1673 && export PATH=/mnt/code/Source-home/GitHub/AIFactory/apps/backend/.venv/bin:$PATH`
- Never mix `tests/...` and `apps/web-server/tests/...` in one pytest command (DV5).
- Tests set ENV only with `monkeypatch.setenv` / `monkeypatch.delenv`. Every
  new or extended test module gets an autouse fixture
  `monkeypatch.delenv(ENV, raising=False)` so a stray shell value cannot turn
  legacy fixtures into holds.
- Ruff: no aliased names inside a multi-name import; every `# noqa` sits on a
  single-line import, never a parenthesised one. CI's default ruff and
  `scripts/cq_ratchet.py --staged` disagree on import sorting; run both.
  Both configs select `I`, and isort **merges** separate `from X import a` /
  `from X import b` statements into one (checked: I001 under both configs).
  So use exactly one unaliased `from X import a, b` per module, and keep it
  within 88 columns so the formatter never wraps it in parentheses.
- The strict ratchet selects `BLE` and `PL`: each new `except Exception`
  carries `# noqa: BLE001 - <reason>` (pattern of `trusted_contract.py:92`),
  each lazy import `# noqa: PLC0415`.
- Patchability: names tests patch must be looked up at call time —
  `spawn_env` calls module-global `lookup` (top-level import in
  `trusted_contract.py`); `deploy_scaffold` imports `trusted_contract` at top
  level as a single-name import; `contract_trust.trusted_contract` calls
  `has_trusted_trace` by its module-global name; `stamp_spawn` and `spawn_env`
  stay lazy imports inside `build_backend` / `agent_service`.
- Autonomy matrix: `build_backend.py` and `agent_service.py` lines shift and
  docs may cite them. Run `python scripts/gen_autonomy_matrix.py --check`; if
  stale, run it without `--check` and commit the output in the same commit.
  Keep the `_STRIP_VARS` edit on one line.
- Commit scope never contains `#`: `test(contract): ... (#1673)`,
  `fix(contract): ... (#1673)`, `docs(contract): ... (#1673)`. End every
  commit with the session attribution trailers.
- Hand-off: four steps edit files and more than three files change, so once
  this plan is approved it goes to the `coder` agent: step 1 first, each later
  step to the same agent via `SendMessage`. Review with a fresh opus agent
  given only this plan path and `git diff`.

## Steps

1. **Failing tests, no production code.**
   `tests/test_contract_trust.py` (new), `tests/test_deploy_scaffold.py` (append
   at the end, after :70), `tests/test_migration_mapper.py` (append),
   `tests/test_trusted_contract_isolation_stamp.py` (:130-175 + append),
   `apps/web-server/tests/test_trusted_contract_merge.py` (append),
   `tests/test_child_process_env.py` (append near :87): write every test listed
   under **Tests** below → verify by
   `python -m pytest tests/test_contract_trust.py tests/test_deploy_scaffold.py tests/test_migration_mapper.py tests/test_trusted_contract_isolation_stamp.py tests/test_child_process_env.py -q`
   and `python -m pytest apps/web-server/tests/test_trusted_contract_merge.py -q -o asyncio_mode=auto`:
   new tests red (import errors / missing behaviour), and the extended
   `test_12_kubejob_dispatch_stamps_before_job_is_created` red; every other
   pre-existing test still green.
   Traps: autouse ENV delete in each module; before writing, `grep -rn "has_trusted_trace\|_read_json" tests apps/web-server/tests`
   (today nothing patches them; keep it so); leave the seven existing
   deploy_scaffold tests (:13-70) untouched (their fixture has no `approval` key → legacy);
   commit `test(contract): failing tests for the trusted contract gate (#1673)`.

2. **`apps/backend/core/contract_trust.py` (new), `apps/web-server/server/services/trusted_contract.py:9-13, :30-46`, `apps/backend/core/migration_mapper.py:31-35`:**
   create `contract_trust.py` per the design (ENV, `contract_digest`, moved
   `_read_json` + `has_trusted_trace`, `trusted_contract` per the truth table,
   whole body in `try/except Exception` → `("hold", None, reason)`); in the
   server module delete :30-46, add `import sys`, and below the existing
   `trusted_contract_store` import add the DV6 guard (copy
   `subprocess_env.py:22-24`) followed by
   `from core.contract_trust import has_trusted_trace  # noqa: E402` so the name
   stays importable there (keep `import json`: `_record_verified` at :59 uses
   it); change only the `load_contract` docstring → verify by
   `python -m pytest tests/test_contract_trust.py -q` (green),
   `(cd apps/web-server && env -u PYTHONPATH PYTHONPATH=. python -c "import server.services.trusted_contract")`
   (exits 0; fails without the guard),
   `python -m pytest apps/web-server/tests -q -o asyncio_mode=auto` and
   `python -m pytest tests -q` (pre-existing green, since about twenty root
   test modules import this server module; only the new `spawn_env`, kubejob
   and in-pod tests still red), and the D2 `git diff` check prints nothing.
   Traps: import `migration_mapper.load_contract` and `trusted_plan._canonical`
   lazily inside functions (each its own single-line import with
   `# noqa: PLC0415`) so the server's module-level import stays light and
   cycle-free; detect "unset" with `ENV not in os.environ`, not falsiness
   (`""` must hold); read the env inside the function; compare against the
   digest of the same object that is returned.

3. **`apps/backend/cli/build_commands.py:8-11, :410-429` and `apps/backend/agents/deploy_scaffold.py:17-22, :79-92`:**
   - `build_commands`: add `import logging` and module-level
     `logger = logging.getLogger(__name__)`. Add module-level
     `_resolve_build_contract(spec_dir) -> dict | None` (DV3): calls
     `trusted_contract(spec_dir)`; on `hold` with `is_migration(contract)`:
     `logger.warning(...)` plus
     `print_status(f"[trusted-contract] migration contract held ({why}); build stopped", "error")`
     then `sys.exit(1)` (D10: every hold logs); on any other `hold`: `logger.warning(...)` plus
     `print_status(f"[trusted-contract] contract held ({why}); normal build", "warning")`,
     return None; otherwise return the contract. In the try at :410-427 drop
     `load_contract` from the import (leaving `is_migration`,
     `prepare_migration_workspace`) and replace :417 with
     `_contract = _resolve_build_contract(spec_dir)`. Leave
     `except Exception` at :428 as is (it does not catch `SystemExit`).
   - `deploy_scaffold`: remove `import json` (:19, else ruff F401); add
     `import logging`, `logger = logging.getLogger(__name__)`,
     `from ui import print_status` (pattern of `coder.py:52`) and
     `from core.contract_trust import trusted_contract`. Rewrite
     `scaffold_deploy_for_spec`: `state, data, why = trusted_contract(spec_dir)`;
     on `hold` log `logger.warning` and
     `print_status(f"[trusted-contract] deploy scaffold withheld: {why}", "warning")`,
     return `[]`; else
     `return scaffold_deploy(data.get("deployment") if data else None, _worktree_root(spec_dir))`.
     Whole body in `try/except Exception: return []` (never-raise kept).
     `coder.py:1092` call unchanged.
   → verify by `python -m pytest tests/test_deploy_scaffold.py tests/test_migration_mapper.py tests/test_contract_trust.py tests/test_solo_mode.py -q` (green), then `python -m pytest tests -q`.
   Traps: keep each noqa on a single-line import; if any test imports
   `agents.deploy_scaffold` without `ui` importable, move the `print_status`
   import inside the function; `tests/test_no_unscrubbed_spawn.py` is
   unaffected (no new spawn); commit `fix(contract): gate deploy scaffold and migration on the server verdict (#1673)`.

4. **`apps/web-server/server/services/trusted_contract.py:13, :63-68`, `apps/web-server/server/services/build_backend.py:1258-1288`, `apps/web-server/server/services/agent_service.py:1235-1237`, `apps/backend/core/child_env.py:16`:**
   - `trusted_contract.py` (numbers below are before step 2; after it,
     `handoff_contract` sits about 16 lines higher): extend the existing
     imports to exactly
     `from server.services.trusted_contract_store import LOOKUP_FAILED, TrustedRecord, lookup`
     and `from core.contract_trust import ENV, contract_digest, has_trusted_trace  # noqa: E402`
     (one statement per module, see the ruff trap; both fit in 88 columns); add
     `async def spawn_env(spec_dir: Path) -> dict[str, str]` beside
     `handoff_contract` per the design.
   - `build_backend.py`: move the `stamp_spawn` lazy import and
     `await stamp_spawn(spec_dir_for(project_path, spec_id), "kubejob")`
     (:1282-1285, with its #1667 comment) above `extra_env = build_job_env(oauth_token)`
     (:1260); right after that line add
     `from .trusted_contract import spawn_env  # noqa: PLC0415` and
     `extra_env.update(await spawn_env(spec_dir_for(project_path, spec_id)))`.
     Do not edit `build_job_env` (:330-351, whitelist).
   - `agent_service.py`: between the stamp (:1235) and
     `create_subprocess_exec` (:1237) add the lazy `spawn_env` import and
     `env.update(await spawn_env(spec_dir))`.
   - `child_env.py:16`: append `"AIFACTORY_TRUSTED_CONTRACT"` to `_STRIP_VARS`
     on the same line (DV1).
   → verify by `python -m pytest tests/test_trusted_contract_isolation_stamp.py tests/test_child_process_env.py tests/test_no_unscrubbed_spawn.py tests/test_build_backend_kubejob.py tests/test_trusted_contract_store.py -q`,
   `python -m pytest tests -q`,
   `python -m pytest apps/web-server/tests -q -o asyncio_mode=auto`,
   `python scripts/gen_autonomy_matrix.py --check`, and the D2 `git diff` check.
   Traps: on both paths the stamp runs before ENV is minted; both spawns keep
   `env=` built by `make_subprocess_env` (`test_no_unscrubbed_spawn`); the
   commit message notes the strip now also applies to git/gh/in-process
   children (harmless); commit `fix(contract): pass the server verdict to run.py at spawn (#1673)`.

5. **`CHANGELOG.md:1` (`[Unreleased]`, under `### Security`), this plan:**
   CHANGELOG entry for the contract gate, including the release note the spec
   requires: "migration builds stop on in-pod and sandbox hosts until #1680;
   held deploy scaffolds are withheld"; name `AIFACTORY_TRUSTED_CONTRACT` as
   server-set per spawn and stripped from inherited env. No
   `environment-reference.md` row: the spec does not ask for one, and the
   variable is not operator-settable. Update this plan for any further deviation → verify by
   `ruff format --check apps/backend apps/web-server scripts tests`,
   `ruff check apps/backend apps/web-server scripts tests`,
   `git add -A && python scripts/cq_ratchet.py --staged`,
   `python -m pytest tests -q`, `python -m pytest apps/web-server/tests -q -o asyncio_mode=auto`.
   Traps: PR to `dev` links intent, spec and plan, says which steps the coder
   did, and lists the four follow-up issues (sibling readers adopt
   `trusted_contract()` (D1); task-status hold field (D10); kubejob
   integration check (D11); explicit `legacy` value); commit
   `docs(contract): changelog for the contract gate (#1673)`.

## Tests

### A. `tests/test_contract_trust.py` (new)

`sys.path.insert(0, <repo>/apps/backend)`; `from core import contract_trust as ct`
and `from core.contract_trust import ENV, contract_digest, trusted_contract`
(no alias inside the multi-name import). Helper `_write(spec, obj, **dumps_kw)`
writes `spec/context/task_contract.json`.
`C = {"deployment": {"deploy_system": "gcp-cloud-run"}, "feature": "f"}`.

| Test | Setup | Expected |
|---|---|---|
| `test_verified_returns_the_contract` | write C; ENV = `contract_digest(C)` | `("verified", C, _)` |
| `test_verified_survives_reformatting` | write C keys reversed, `indent=4`; ENV = digest of C built in another key order | `verified`, contract == C |
| `test_env_hold_holds_by_server` | write C; ENV=`hold` | `("hold", C, "held by server")` |
| `test_digest_mismatch_holds` | write C; ENV = digest of C with `deploy_system` changed | `("hold", _, "digest mismatch")` |
| `test_bad_env_value_holds` [`""`, `"zz"`, `digest.upper()`] | write C | `hold`, `digest mismatch` |
| `test_missing_file_under_digest_holds` | no file; ENV = digest of C | `("hold", None, "digest mismatch")` |
| `test_missing_or_bad_file_is_silent_legacy` [file: none, `"[1]"`, `"{"`] × [ENV unset, `hold`] | — | `("legacy", None, _)`; caplog has no `[trusted-contract]` |
| `test_trace_without_env_holds` [contract with `approval`; `requirements.json` with `provenance.trusted_plan=True`] | ENV unset | `hold`, `trusted trace without a server verdict` |
| `test_no_env_no_trace_is_legacy` | write C; ENV unset | `("legacy", C, _)` |
| `test_exception_holds` | write C; patch `ct.has_trusted_trace` to raise `RuntimeError` | `hold`, nothing escapes |
| `test_env_read_at_call_time` | call unset (legacy), then `setenv(ENV, "hold")`, call again | `legacy` then `hold` |
| `test_has_trusted_trace_sources` | approval in contract; in `implementation_plan.json`; requirements provenance; none | True, True, True, False |

### B. `tests/test_deploy_scaffold.py` (extend)

Autouse ENV delete; helper `_spec(tmp_path, obj)` writing under
`.aifactory/specs/001-game`.
- `test_scaffold_for_spec_verified_digest_scaffolds`: ENV = digest; `infra/main.tf`
  returned and exists at the worktree root.
- `test_scaffold_for_spec_withheld` [ENV=`hold`; ENV = digest of another dict;
  ENV unset + `approval` in contract]: returns `[]`, `tmp_path/"infra"` absent,
  caplog (WARNING, logger `agents.deploy_scaffold`) contains
  `[trusted-contract] deploy scaffold withheld`.
- `test_scaffold_for_spec_never_raises`: patch `deploy_scaffold.trusted_contract`
  to raise → `[]`.

### C. `tests/test_migration_mapper.py` (extend)

Drive `cli.build_commands.handle_build_command`, copying the patch list from
`tests/test_solo_mode.py:376-400` (`cli.utils.validate_environment`,
`cli.utils.print_banner`, `agent.sync_plan_to_source`, `ReviewState` on the
module object, `get_existing_build_worktree` → None, `choose_workspace` →
`WorkspaceMode.ISOLATED`, `setup_workspace` → `(tmp_path/"wt", None, spec_dir)`,
`agent.run_autonomous_agent` as `AsyncMock()`,
`core.migration_mapper.prepare_migration_workspace` as `MagicMock(return_value={})`),
all four module-bound names (`choose_workspace`, `get_existing_build_worktree`,
`setup_workspace`, `ReviewState`) via `patch.object(build_commands, ...)`.
Call it with the full required kwargs as `test_solo_mode.py:406-417` does
(`model`, `max_iterations=1`, `verbose=False`, `force_isolated=True`,
`force_direct=False`, `auto_continue=True`, `skip_qa=True`,
`force_bypass_approval=False`) plus `stop_after_planning=True`. Autouse:
`monkeypatch.setenv("AIFACTORY_AUTH_PREFLIGHT", "off")`, otherwise
`build_commands.py:271-276` runs a live credential probe. caplog captures
WARNING on logger `cli.build_commands`. Migration contract = existing `_contract()`.
- `test_build_held_migration_exits_before_agent`: ENV=`hold` → `SystemExit`
  code 1; neither the agent nor `prepare` called.
- `test_build_held_non_migration_runs_without_contract`: ENV=`hold`, contract
  `{"feature": "f"}` → no exit, `prepare` not called, agent called once, caplog
  has `[trusted-contract] contract held`.
- `test_build_verified_migration_prepares_workspace`: ENV = digest of
  `_contract()` → `prepare` called once with `(tmp_path/"wt", project_dir, _contract())`.
- `test_build_legacy_migration_unchanged`: ENV unset, no approval → `prepare` called.

### D. `apps/web-server/tests/test_trusted_contract_merge.py` (extend, async)

Reuse `_isolated_host`, `_spec`, `_rec`, `_sign`, `LOOKUP_FAILED`; patch
`monkeypatch.setattr("server.services.trusted_contract.lookup", fake)` with an
`async def` fake.
- `test_spawn_env_verified_is_digest`: lookup → `_rec(signed)` → `{ENV: contract_digest(signed)}`.
- `test_spawn_env_holds` [LOOKUP_FAILED; `_rec(signed, "none")`; `_rec(signed)`
  with the disk contract edited; None with a trace; `_rec(signed)` with
  `AIFACTORY_BUILD_BACKEND=subprocess`] → `{ENV: "hold"}`.
- `test_spawn_env_legacy_is_empty`: bare spec dir, lookup None → `{}`.
- `test_cross_side_digest_matches_pod_verdict`: lookup → `_rec(signed)`; `_spec`, rewrite disk contract
  keys reversed `indent=2`; `env = await spawn_env(spec)`;
  `monkeypatch.setenv(ENV, env[ENV])`; `trusted_contract(spec)` →
  `("verified", signed, _)`.
- `test_has_trusted_trace_is_the_core_predicate`:
  `trusted_contract.has_trusted_trace is contract_trust.has_trusted_trace`.

### E. `tests/test_trusted_contract_isolation_stamp.py` (extend)

- `test_12_kubejob_dispatch_stamps_before_job_is_created` (:130): keep
  `stamped_at_create == ["kubejob"]`; wrap `trusted_contract_store.stamp_spawn`,
  `bb.build_job_env`, `trusted_contract.spawn_env`, `_Batch.create_namespaced_job`
  to append to `order`; assert `order == ["stamp", "env", "spawn_env", "create"]`
  and the manifest container env has ENV = `hold` (unsigned record `{"feature": "f"}`).
- `test_kubejob_legacy_and_stray_server_env_carry_no_value`: no `store.put`,
  `setenv(ENV, "forged")` → ENV absent from the manifest env.
- `_spawn` helper gains optional `envs: list | None`; `_fake_exec` appends
  `kw["env"]` when given (existing callers unaffected).
- `test_inpod_spawn_merges_value_after_stamp`: `store.put({"feature": "f"})`,
  `setenv(ENV, "forged")` → order stamp then `spawn_env`, captured `env[ENV] == "hold"`.
- `test_inpod_legacy_spawn_carries_no_value`: no record, `setenv(ENV, "forged")`
  → ENV not in captured env.

### F. `tests/test_child_process_env.py` (extend)

- `test_make_subprocess_env_drops_trusted_contract`: `setenv(ENV, "x")` → ENV in
  neither `make_subprocess_env()` nor `child_env()`.
- `test_extra_trusted_contract_survives_strip`:
  `make_subprocess_env(extra={ENV: "d"})[ENV] == "d"`.

### Commands and expected result

```
cd /mnt/code/Source-home/GitHub/AIFactory-1673
export PATH=/mnt/code/Source-home/GitHub/AIFactory/apps/backend/.venv/bin:$PATH
python -m pytest tests/test_contract_trust.py tests/test_deploy_scaffold.py tests/test_migration_mapper.py \
  tests/test_trusted_contract_isolation_stamp.py tests/test_child_process_env.py tests/test_trusted_contract_store.py \
  tests/test_build_backend_kubejob.py tests/test_no_unscrubbed_spawn.py tests/test_solo_mode.py \
  tests/test_constitution_prompt.py -q
python -m pytest apps/backend/prompts_pkg/test_deployment_prompt.py -q
python -m pytest apps/web-server/tests -q -o asyncio_mode=auto
python -m pytest tests -q
ruff format --check apps/backend apps/web-server scripts tests
ruff check apps/backend apps/web-server scripts tests
git add -A && python scripts/cq_ratchet.py --staged
python scripts/gen_autonomy_matrix.py --check
git diff --name-only origin/dev...HEAD -- apps/backend/trusted_plan.py apps/backend/pfactory/tfactory_client.py \
  apps/backend/core/auth.py apps/backend/core/client.py
```

Expected: every pytest run 0 failed, no new skips; ruff, ratchet and matrix
check exit 0; the D2 diff prints nothing.

### Mutation checks (manual, once each, then revert)

| Mutation | Test that must fail |
|---|---|
| digest `==` → `!=`, or verified whenever ENV is set | `test_verified_returns_the_contract` / `test_digest_mismatch_holds`, `test_bad_env_value_holds` |
| `contract_digest` uses `json.dumps` not `_canonical` | `test_verified_survives_reformatting`, `test_cross_side_digest_matches_pod_verdict` |
| `if not os.environ.get(ENV)` instead of presence check | `test_bad_env_value_holds[""]` |
| env read hoisted to module level | `test_env_read_at_call_time` |
| missing file → legacy for any env | `test_missing_file_under_digest_holds` |
| hold logged on missing file | `test_missing_or_bad_file_is_silent_legacy` |
| drop `has_trusted_trace` branch | `test_trace_without_env_holds`, deploy `withheld[trace]` |
| `except Exception` returns legacy | `test_exception_holds` |
| delete deploy `hold → []` | `test_scaffold_for_spec_withheld` (all) |
| remove deploy outer try | `test_scaffold_for_spec_never_raises` |
| delete `sys.exit(1)` | `test_build_held_migration_exits_before_agent` |
| hold branch ignores `is_migration` (every hold warns and continues) | `test_build_held_migration_exits_before_agent` |
| drop the warning on a non-migration hold | `test_build_held_non_migration_runs_without_contract` (caplog) |
| widen except at :428 to `BaseException` | `test_build_held_migration_exits_before_agent` |
| `spawn_env` hold → `{}` | `test_spawn_env_holds` |
| `spawn_env` legacy → `{ENV: "hold"}` | `test_spawn_env_legacy_is_empty`, `test_inpod_legacy_spawn_carries_no_value` |
| stamp left after `build_job_env` | `test_12_...` order |
| drop `extra_env.update(...)` | `test_12_...` manifest |
| `env.update(spawn_env)` before stamp | `test_inpod_spawn_merges_value_after_stamp` |
| ENV removed from `_STRIP_VARS` | `test_make_subprocess_env_drops_trusted_contract`, `test_inpod_legacy_spawn_carries_no_value` |
| strip applied after `extra` | `test_extra_trusted_contract_survives_strip` |
| local `has_trusted_trace` copy re-added in server | `test_has_trusted_trace_is_the_core_predicate` |

## Rollback

1. Nothing new is persisted: the value is an env var computed per spawn, no DB
   column, nothing in the spec dir. Rollback is `git revert <merge-sha>` (or
   each commit newest first) on a branch, PR to `dev`.
2. After the revert: `python scripts/gen_autonomy_matrix.py` then `--check`,
   commit regenerated outputs if changed; run the pytest and ruff commands
   above; add a CHANGELOG `[Unreleased]` entry recording the revert.
3. In-flight builds keep the env they were spawned with. Old-code pods ignore
   the variable; new-code pods with no variable fall back to
   `has_trusted_trace`, so a mixed rollout cannot fail open.
4. Partial rollback (only the migration stop misbehaves): revert the
   `build_commands.py` hunk and its tests; `contract_trust`, `deploy_scaffold`
   and the server side stay. Update this plan in the same commit.

## Deviations

- Step 1: added `test_build_held_migration_never_calls_agent_or_prepare`
  (asserts the agent and `prepare` are never called on a held migration);
  a stricter twin of `test_build_held_migration_exits_before_agent`.
- Step 1: `test_extra_trusted_contract_survives_strip` is green on arrival
  (`child_env` applies `extra` after the strip); kept as a regression guard.
