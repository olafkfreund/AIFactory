---
status: approved
issue: 1988
spec: spec/2026-10-31-1988-qa-test-module-eviction.md
coders:
  1: codex   # the lead assigned codex to the fix step for this wave
---

# Plan: stop test_qa_criteria leaking mocked modules into other test files

Decisions carried over from the approved spec:

- Test-only change in `tests/test_qa_criteria.py`. No production code.
- Fix the offender: collection-time setup must leave `sys.modules` as it
  found it; the mocks and mock-bound `qa.*` modules are installed only while
  this file's tests run, via a module-scoped autouse fixture using
  `pytest.MonkeyPatch.context()`.
- Victim tests (`tests/test_git_metadata_baseline.py`) stay unchanged.
- Regression test is the pair run
  `pytest tests/test_git_metadata_baseline.py tests/test_qa_criteria.py`
  (fails on dev, passes with the fix). No new test code.
- `tests/test_spec_pipeline.py` and `tests/qa_report_helpers.py` have the
  same pattern; not changed here, follow-up.

## Steps

1. `tests/test_qa_criteria.py`:
   - Lines ~28-41: replace `_original_modules` / `_mocked_module_names` and
     their loop with `_modules_before = dict(sys.modules)`, plus a one-line
     comment naming #1988.
   - After the `from qa.criteria import (...)` block (~line 128), compute
     `_module_changes = {name: sys.modules.get(name) for name in
     set(_modules_before) | set(sys.modules) if sys.modules.get(name) is not
     _modules_before.get(name)}`, then restore each name: put back
     `_modules_before[name]` if it existed, else `del sys.modules[name]`.
   - Lines ~143-151: replace `cleanup_mocked_modules` with a module-scoped
     autouse fixture `mocked_modules` that, inside
     `pytest.MonkeyPatch.context() as mp`, calls
     `mp.delitem(sys.modules, name, raising=False)` for `None` values and
     `mp.setitem(sys.modules, name, module)` otherwise, then yields.
   → verify by `apps/backend/.venv/bin/pytest tests/test_qa_criteria.py -q`
   (all pass) and
   `apps/backend/.venv/bin/pytest tests/test_git_metadata_baseline.py tests/test_qa_criteria.py -q`
   (all pass; on origin/dev two coder tests fail).
   Traps: keep the existing mock objects (`mock_progress`, `mock_report`, ...)
   at module level; tests mutate them directly. `rootdir` is `tests/`.

## Tests

- Step 1 checks.
- Full `apps/backend/.venv/bin/pytest tests/ -m "not slow" -q`: only the three
  local-gh failures (`test_github_env` x2, `test_marker_nonce`) remain.
- `ruff check` / `ruff format --check` on the file.

## Rollback

Revert the commit; test-only.
