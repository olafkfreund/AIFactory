---
status: approved
issue: 1711
spec: spec/2026-10-15-1711-trusted-contract-legacy.md
---

# Plan: an explicit legacy verdict, and a kubejob check that the pod acts on it

Closes #1711 and #1710. Approved decisions carried from the spec:

- D1: the server's `spawn_env` sends `AIFACTORY_TRUSTED_CONTRACT=legacy` for the
  legacy state (was: unset). Constant `LEGACY = "legacy"` lives in
  `apps/backend/core/contract_trust.py`.
- D2: the pod's `trusted_contract`: no contract file + `legacy` → `("legacy",
  None, "no contract")`; a contract file + `legacy` → `("hold", contract,
  "server saw no signed contract")`. ENV unset keeps today's trace fallback.
  Every other row unchanged.
- D3: `prompts._load_contract` returns `None` silently under `legacy` (no
  `implementation_plan.json` fallback, no warning).
- D4: one parametrized kubejob integration test: the server verdict reaches the
  Job manifest and the pod's `_resolve_build_contract` acts on it.

Environment: `export PATH=/mnt/code/Source-home/GitHub/AIFactory/apps/backend/.venv/bin:$PATH`
in the worktree. Never create a `.venv` in the worktree.

## Steps

1. **Pod reads `legacy`.** `apps/backend/core/contract_trust.py`
   - `:3-5` docstring: "the sha256 of the verified contract, `hold`, or
     `legacy`; unset means no server".
   - after `ENV = ...` (`:16`): `LEGACY = "legacy"`.
   - `_REASON_LABELS` (`:21-31`): add `"server saw no signed contract"`.
   - `:78`: `if not present or value in ("hold", LEGACY):`.
   - after the `value == "hold"` branch (`:81-82`), before `if present:`:
     `if value == LEGACY: return "hold", contract, "server saw no signed contract"`.
   - Tests, `tests/test_contract_trust.py`: extend the `env` parametrize of
     `test_missing_or_bad_file_is_silent_legacy` (`:86`) with `"legacy"`; add
     `test_legacy_env_holds_a_contract_file` (write a contract with no
     `approval`, `setenv(ENV, "legacy")` → `("hold", C, "server saw no signed
     contract")`, and `reason_label` of that reason is the reason itself).
   - Check: `pytest -q tests/test_contract_trust.py`
   - Traps: keep `present = ENV in os.environ` (unset is detected by presence,
     not falsiness). The function's `# noqa: PLR0911` stays.

2. **Server sends `legacy`.** `apps/web-server/server/services/trusted_contract.py`
   - `:20`: import `LEGACY` with `ENV`.
   - `:59-64`: docstring "a digest if verified, `hold`, or `legacy`"; last line
     `return {ENV: "hold"} if state == "hold" else {ENV: LEGACY}`.
   - Tests:
     - `apps/web-server/tests/test_trusted_contract_merge.py:534-540`: rename to
       `test_spawn_env_legacy_is_explicit`, expect `{ENV: "legacy"}`.
     - `tests/test_trusted_contract_isolation_stamp.py:232-239`: rename to
       `test_kubejob_legacy_overrides_stray_server_env`, expect
       `env[_ENV] == "legacy"`; `:268-274` rename to
       `test_inpod_legacy_spawn_sends_legacy`, expect `envs[0][_ENV] == "legacy"`.
   - Check: `pytest -q apps/web-server/tests/test_trusted_contract_merge.py tests/test_trusted_contract_isolation_stamp.py`
   - Traps: the stray `forged` server value must still not leak (the assert
     on `legacy` proves the override).

3. **Prompt readers stay quiet under `legacy`.** `apps/backend/prompts_pkg/prompts.py:596`
   - before `if contract_trust.ENV in os.environ or ...`, add
     `if os.environ.get(contract_trust.ENV) == contract_trust.LEGACY:` /
     `return None  # the server vouched for no contract`.
   - Test, `apps/backend/prompts_pkg/test_house_standards_prompt.py`:
     `test_legacy_ignores_plan_fallback_quietly`: standards in
     `implementation_plan.json` (no `approval`), `setenv(ENV, "legacy")` →
     `""` and `"implementation_plan.json is not verified" not in caplog.text`.
   - Check: `pytest -q apps/backend/prompts_pkg/`
   - Traps: none.

4. **Kubejob integration check (#1710).** `tests/test_trusted_contract_isolation_stamp.py`
   - Import `from cli.build_commands import _resolve_build_contract` (the
     file already puts `apps/backend` on `sys.path`).
   - `test_kubejob_verdict_reaches_pod_build` parametrized over
     `(state, pod_file, expect)`: `("verified", True, "contract")`,
     `("hold", True, "exit")`, `("legacy", True, "exit")`,
     `("legacy", False, "none")`.
     - Contract `C = {"feature": "f", "change_mode": "migration"}`.
     - Patch `tcm.resolve_contract` to return `(state, C if state == "verified" else {})`
       so `spawn_env` computes the real value (digest via `contract_digest`).
     - Run `_kubejob_dispatch(..., record=None)`; take `value = env[_ENV]`.
     - Pod spec dir `tmp_path / "pod-spec"`; if `pod_file`, write
       `context/task_contract.json` = `C`.
     - `monkeypatch.setenv(_ENV, value)`; then `contract` → `_resolve_build_contract(spec) == C`;
       `exit` → `pytest.raises(SystemExit)` with code 1; `none` → returns `None`.
   - Check: `pytest -q tests/test_trusted_contract_isolation_stamp.py`; the
     legacy rows fail on the pre-step-2 code (no value in the manifest).
   - Traps: `_kubejob_dispatch` itself monkeypatches `tcm.spawn_env` with a
     wrapper over the real one, so patch `resolve_contract`, not `spawn_env`.

## Tests

```
pytest -q tests/test_contract_trust.py tests/test_trusted_contract_isolation_stamp.py \
  apps/web-server/tests/test_trusted_contract_merge.py apps/backend/prompts_pkg/ \
  tests/test_migration_mapper.py tests/test_deploy_scaffold.py tests/test_constitution_prompt.py
ruff check <touched files>; ruff format --check <touched files>
mypy apps/backend/core/contract_trust.py
```
All pass.

## Rollback

Revert the PR. Server and pod revert together (one image).
