---
status: approved
issue: 1932
spec: spec/2026-10-27-1932-in-pod-agent-isolation.md
---

# Plan: slice C, fail closed when the build backend can't run

Scope: slice C only. Slices A1-A3, B and D are in the spec and get their own plans.

Approved decisions carried over from the spec:

- `build_backend.backend_config_error(store_enabled) -> str | None`: reads
  `AIFACTORY_BUILD_BACKEND` (strip, lower, empty = unset = `subprocess`).
  Returns `None` for subprocess, and for kubejob with the store enabled.
  Returns a message for an unknown value (name the value and the valid ones)
  and for kubejob without `DATABASE_URL` (name the variable).
- `BuildBackendUnavailableError(RuntimeError)` is raised with that message at
  exactly two points: `_start_build_unit` (covers start, in-memory drain and
  durable drain) and `start_spec_creation` before the subprocess spawn. Spec
  creation needs its own check because kubejob-without-store drops
  `--no-build`, and spec_runner then runs the whole build in-pod.
- Read-side paths stay as they are: `_kubejob_backend_enabled`, stop,
  reconcile, reap, `trusted_contract`, and `selected_backend()` returning
  `subprocess` for unknown values. Only their log text changes from "falling
  back" to "agent launches are refused".
- Startup: `build_backend.log_backend_config(logger, store_enabled)`, called in
  the `main.py` lifespan. On a config error it logs ERROR and the server keeps
  running. With subprocess and `KUBERNETES_SERVICE_HOST` set it logs WARNING
  (in-cluster agents run unisolated, set kubejob + DATABASE_URL, #1932).
  Otherwise it logs nothing new.
- No route or HTTP status changes. Existing `except Exception` →
  `client_error` mapping stays.
- Prod is unaffected: factory-gitops sets kubejob and `DATABASE_URL` on the
  `aifactory` container (checked read-only on 2026-10-27).

## Steps

1. `apps/web-server/server/services/build_backend.py`
   - Module docstring ~9-12: say unknown values and kubejob without
     `DATABASE_URL` refuse agent launches (#1932).
   - Near `selected_backend` (~373): add `BuildBackendUnavailableError`,
     `backend_config_error`, `log_backend_config`. Share the env parse with
     `selected_backend` through one small `_raw_backend()` helper. In
     `selected_backend`, treat empty as the default and change the unknown-value
     warning text (it still returns `subprocess`).
   - `tests/test_build_backend_kubejob.py` §2 (~505-530): add tests for
     `backend_config_error` (unset, empty, subprocess, kubejob+store, kubejob
     without store, unknown) and for `log_backend_config` (error case, and the
     in-cluster warning with `KUBERNETES_SERVICE_HOST` set/unset) using `caplog`.
   → verify by `cd apps/backend && .venv/bin/pytest ../../tests/test_build_backend_kubejob.py -q`
   Traps: `_log` is the module logger. Keep the existing unknown-value test
   passing.

2. `apps/web-server/server/services/agent_kubejob.py`
   - `_start_build_unit` (~491): the first statement raises
     `BuildBackendUnavailableError(f"Builds refused: {problem}")` when
     `backend_config_error(self._store_enabled)` returns a message.
   - `_kubejob_backend_enabled` (~145-161): update the docstring and warning
     text. No longer "falling back"; launches are refused by `_start_build_unit`.
     Return value unchanged.
   - `tests/test_agent_service_kubejob_backend.py`: add tests for kubejob with
     `_store_enabled=False`, and for `AIFACTORY_BUILD_BACKEND=lambda` with
     `_store_enabled=False`. In each, `start_task_execution` raises
     `BuildBackendUnavailableError`, and neither `_spawn_task_execution` nor
     `_dispatch_build_job` runs (monkeypatch both to record calls, as
     `test_default_backend_still_spawns_subprocess` does). Rename
     `test_kubejob_falls_back_to_subprocess_without_store` to
     `test_kubejob_without_store_reports_not_enabled`, keeping its assertion.
   → verify by `.venv/bin/pytest ../../tests/test_agent_service_kubejob_backend.py ../../tests/test_agent_kubejob_mixin.py -q`
   Traps: AgentService reads `_store_enabled` from env at init, so tests set
   it on the instance after construction. The mixin declares sibling attrs in
   its `TYPE_CHECKING` block. Add `_store_enabled: bool` there if mypy needs it.

3. `apps/web-server/server/services/agent_spec_creation.py`
   - `start_spec_creation`: right after the "already running" check, raise
     `BuildBackendUnavailableError(f"Spec creation refused: {problem}")` on a
     config error. Add `_store_enabled: bool` to the mixin's declared attrs.
   - Test (add to `tests/test_agent_spec_creation_mixin.py`, using the
     `test_marker_nonce.py::test_spec_creation_spawns_with_a_nonce` pattern):
     kubejob with `_store_enabled=False` makes `start_spec_creation` raise,
     and the patched `asyncio.create_subprocess_exec` is never called.
   → verify by `.venv/bin/pytest ../../tests/test_agent_spec_creation_mixin.py ../../tests/test_marker_nonce.py -q`
   Traps: `test_marker_nonce` builds `AgentService()` with no env, so the
   default must keep passing.

4. `apps/web-server/server/main.py` ~235 lifespan: call
   `log_backend_config(logger, store_enabled())` before the reconcile-loop `if`.
   Both names are already imported there. `docs/docs/environment-reference.md:241`:
   change the row to say unknown values are refused, kubejob needs
   `DATABASE_URL`, and unset in-cluster logs a warning.
   → verify by `.venv/bin/pytest ../../tests -q -k "build_backend or kubejob or spec_creation or lifespan or startup"`
   Traps: main.py imports inside the lifespan use `# noqa: PLC0415`.

5. Whole-change checks: `ruff check` and `ruff format --check` on the touched
   files, `python scripts/ratchet_lint.py` (mypy ratchet), and grep the tests
   for other `AIFACTORY_BUILD_BACKEND", "kubejob"` setups that expect an
   in-pod spawn without a store. Fix any that encode the old fallback.
   → verify by the commands above, all clean.

## Tests

- New unit tests from steps 1-3 fail on `origin/dev`: the function or error
  doesn't exist there, or the subprocess spawn happens. They pass after the
  change.
- The existing suites for build_backend, agent kubejob, spec creation and the
  marker nonce stay green.
- CI on the PR is green.

## Rollback

Revert the PR. The behaviour goes back to the silent fallback. Nothing is
persisted and there is no config or schema change.
