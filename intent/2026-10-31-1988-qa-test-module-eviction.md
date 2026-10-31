---
status: approved
issue: 1988
author: olafkfreund
---

# Intent: stop test_qa_criteria leaking mocked modules into other test files

## Problem

In a full local `pytest tests/ -m "not slow"` run, three QA tests in
`tests/test_git_metadata_baseline.py` fail with `DID NOT RAISE SystemExit`.
They pass alone. CI passes them.

`tests/test_qa_criteria.py` writes MagicMocks into `sys.modules` (`ui`,
`progress`, `task_logger`, `linear_updater`, `client`, `claude_agent_sdk`) and
pops `qa.*` at import time. Pytest imports every test file during collection,
before any test runs, so those mocks and the mock-bound `qa` package stay in
`sys.modules` for every test that runs before `test_qa_criteria.py`'s own
module teardown. Running only `test_git_metadata_baseline.py` and
`test_qa_criteria.py` already breaks two other tests in the first file: their
`BuildState` is `mock.BuildState`.

## Proposed outcome

`test_qa_criteria.py` changes `sys.modules` only while its own tests run.
The full local run no longer fails the three QA tests, and introduces no new
failures. Test-only change; no production code changes.

## Affected users and systems

Developers running the backend suite locally; `tests/test_qa_criteria.py`.

## Constraints

- Do not change production code.
- Fix the offender, not the victims.
- `test_qa_criteria.py`'s own tests keep passing, alone and in the full run.

## Open questions

None. The lead decided: fix the offender with a fixture or
`monkeypatch.setitem` scoped to its own tests.
