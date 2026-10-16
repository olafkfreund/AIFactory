---
status: approved
issue: 1762
spec: spec/2026-10-16-1762-runner-fail-closed.md
---

# Plan: Runners fail closed when they cannot hide their environ

Approved decisions (from the spec):

- One shared helper `require_non_dumpable()` in
  `apps/backend/core/process_hardening.py` raises
  `SystemExit("prctl(PR_SET_DUMPABLE, 0) failed")` when `make_non_dumpable()`
  returns False. The three runner sites and the server's `_make_non_dumpable()`
  call it. No env opt-out; non-Linux stays a no-op because
  `make_non_dumpable()` already returns True there.
- `child_env()` compares `k.upper()` against `_STRIP_VARS`.
- HOME credential files (#1762 part b) are out of scope; the PR says
  "Part of #1762".

## Steps

1. `apps/backend/core/process_hardening.py` (end of file): add

   ```python
   def require_non_dumpable() -> None:
       """``make_non_dumpable``, failing closed: a process that cannot hide its secrets must not run."""
       if not make_non_dumpable():
           raise SystemExit("prctl(PR_SET_DUMPABLE, 0) failed")
   ```

   Then replace the two-line `if not process_hardening.make_non_dumpable():` /
   `logger.warning("could not make the process non-dumpable; env stays readable")`
   block with `process_hardening.require_non_dumpable()` at
   `apps/backend/core/client.py:619-620`,
   `apps/backend/core/simple_client.py:78-79` and
   `apps/backend/providers/factory.py:322-323`. In
   `apps/web-server/server/main.py:794-797` import `require_non_dumpable`
   instead of `make_non_dumpable` and call it in place of the `if ... raise`.
   → verify by `pytest tests/test_runner_hardening.py apps/web-server/tests/test_server_non_dumpable.py -q`
   Traps: `require_non_dumpable` must look `make_non_dumpable` up as a module
   global (no default-arg binding) so tests that monkeypatch
   `process_hardening.make_non_dumpable` still take effect. Keep the server's
   `sys.path` set-up and `_make_non_dumpable` name (its tests patch it). If
   `logger` becomes unused in a file, ruff will flag it; it is used elsewhere in
   all three today, so leave it.
2. `apps/backend/core/child_env.py:57`: `and k not in _STRIP_VARS` becomes
   `and k.upper() not in _STRIP_VARS`; add a one-line comment why (env names
   are case-sensitive on Linux, so a lowercase copy is a distinct variable).
   → verify by `pytest tests/test_child_process_env.py -q`
   Traps: `_STRIP_VARS` entries are already upper case; do not change them.
3. Tests.
   - `tests/test_runner_hardening.py`: add one parametrised test
     `test_runners_fail_closed` over the three entry points (same calls as the
     existing `test_create_client_hardens`, `test_create_simple_client_hardens`,
     `test_get_provider_hardens`), with `process_hardening.make_non_dumpable`
     monkeypatched to `lambda: False`, asserting `pytest.raises(SystemExit)`.
   - `tests/test_child_process_env.py`: add
     `test_strip_vars_case_insensitive` using `monkeypatch.setenv("anthropic_api_key_file", "x")`
     asserting the name is absent from `child_env()` and `child_env(runner=True)`.
     `child_env` there comes from `server.utils.subprocess_env`; check it
     accepts `runner=` (else import from `core.child_env`).
   → verify by `pytest tests/test_runner_hardening.py tests/test_child_process_env.py apps/web-server/tests/test_server_non_dumpable.py -q`
   and prove each new test fails against `origin/dev` source files.
   Traps: none beyond the file's existing `# ruff: noqa` header.

## Tests

From the worktree, with `<main clone>/apps/backend/.venv/bin` first on PATH:

- `pytest tests/test_runner_hardening.py tests/test_child_process_env.py apps/web-server/tests/test_server_non_dumpable.py -q` → all pass.
- `ruff check` and `ruff format --check` on the touched files → clean.

## Rollback

Revert the PR. No data, config or migration involved.
