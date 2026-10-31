---
status: draft
issue: 1988
intent: intent/2026-10-31-1988-qa-test-module-eviction.md
---

# Spec: stop test_qa_criteria leaking mocked modules into other test files

## Design

Chosen (the lead's decision): fix the offender, `tests/test_qa_criteria.py`.

1. Before its module-level setup, snapshot `sys.modules`
   (`_modules_before = dict(sys.modules)`).
2. Keep the existing setup unchanged: install the mocks, evict `qa.*`,
   `from qa.criteria import ...`.
3. Right after that import, record every entry the setup added, replaced or
   evicted (`_module_changes`: name -> module, or `None` when evicted), then
   put `sys.modules` back to the snapshot for those names. Collection leaves
   no trace.
4. Replace the `cleanup_mocked_modules` fixture with a module-scoped autouse
   fixture that re-applies `_module_changes` through
   `pytest.MonkeyPatch.context()` (`setitem` / `delitem`) for this file's
   tests and undoes it afterwards. The file's tests see the same modules as
   today, so `print_qa_status`'s lazy `from .report import` and the
   `import qa.report` in two tests keep resolving to the mock-bound package.
5. Regression test: the pair run
   `pytest tests/test_git_metadata_baseline.py tests/test_qa_criteria.py`.
   On origin/dev two coder tests in the first file fail; with the fix all
   pass. No new test code (the lead's decision).

Issue history: the original symptom (three QA tests failing in a full run)
no longer reproduces on origin/dev `89c12286`; the full run shows only the
three local-gh failures. The leak behind it is still there, and the issue was
retitled to it.

Root cause, verified: pytest imports every test file at collection time,
before any test runs. `test_qa_criteria.py` mocks `ui`, `progress`,
`task_logger`, `linear_updater`, `client`, `claude_agent_sdk` and re-imports
`qa.*` under those mocks at import time. Its cleanup ran only at its own
module teardown, so every test file that runs earlier (alphabetically,
including `test_git_metadata_baseline.py`) ran against the mocks and the
mock-bound `qa` package. Running only `test_git_metadata_baseline.py` and
`test_qa_criteria.py` fails two coder tests there: `BuildState` is
`mock.BuildState`. A prototype of this design makes that pair pass (88/88).

## Alternatives rejected

- Patch the three victim tests to resolve `handle_build_command`'s
  collaborators at call time: treats the symptom. Every other test that runs
  before `test_qa_criteria.py` still sees the mocks.
- Move all the mocking into a fixture and import `qa.criteria` inside it:
  rewrites every test's references to the imported names; larger diff for the
  same result.
- Also fix `tests/test_spec_pipeline.py` and `tests/qa_report_helpers.py`.
  They use the same collection-time `sys.modules` pattern but are not
  changed here: scope is `test_qa_criteria.py` only. Follow-up issue.
- A `runpy.run_path(__file__)` regression test inside the file: the pair run
  already proves the fix, without more test code.

## Risks

- `test_qa_criteria.py`'s own tests could depend on a module imported
  outside the snapshot window. Covered by running the file alone and in the
  full run.
- Restoring evicted `qa.*` modules gives later files the `qa` package from
  before collection, which is what they would have had without this file.

## Verification

- Pair run fails on origin/dev (2 coder tests in
  `test_git_metadata_baseline.py`) and passes with the fix.
- `pytest tests/test_qa_criteria.py` alone passes.
- Full `pytest tests/ -m "not slow"` with the Python 3.12 venv: the only
  failures are the three local-gh-credential ones (`test_github_env` x2,
  `test_marker_nonce`), as on origin/dev.
