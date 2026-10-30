---
status: approved
issue: 1960
spec: spec/2026-10-30-1960-repo-scoped-correlation-key.md
---

# Plan: never emit a bare issue number as the correlation key

Decision (approved spec): `<repo>#<n>` when the repo is known, else the
synthetic `af-<spec_id>`; never a bare number. No repo derivation from the
project. The TFactory handoff mirror emits the same key.

## Steps

1. `apps/web-server/server/services/completion.py:230-236` `correlation_key`:
   `return f"{repo}#{issue_number}" if issue_number is not None and repo else f"af-{spec_id}"`;
   update its docstring. In `apps/web-server/tests/test_completion_emitter.py`
   update `test_correlation_key_issue_vs_synthetic` (`correlation_key("spec-9", 412) == "af-spec-9"`)
   and `test_read_issue_repo_parses_the_issue_url_and_falls_back_to_bare`
   (expect `"af-s2"`; rename to `..._falls_back_to_synthetic`).
   → verify by `cd apps/web-server && python -m pytest tests/test_completion_emitter.py -q`
   Traps: grep the web-server tests for other `== "<n>"` bare-key asserts and fix them.
2. `apps/backend/pfactory/tfactory_client.py:586-613`
   `_event_correlation_key(req, spec_id)`: last line returns `f"af-{spec_id}"`
   instead of `str(number)`; `_thread_issue(contract, req, spec_id)` passes it;
   the call at ~734 passes `spec_id`. Update the docstrings. In
   `test_tfactory_handoff_key_matches_the_event_key` pass `"s"`.
   → verify by the same pytest, plus `cd apps/backend && python -m pytest -q -k "tfactory_client or thread_issue"`
   Traps: grep backend tests for `_thread_issue(` callers.

## Tests

`python -m pytest apps/web-server/tests/test_completion_emitter.py -q` green;
ruff clean on touched files.

## Rollback

Revert the PR commit; keys go back to bare numbers.

## Deviations

- Step 1 also updated seven more asserts in `apps/web-server/tests/test_completion_emitter.py`
  that expected a bare key for a spec with no repo (`"412"`, `"1"`); they now
  expect `af-spec-9` / `af-s`.
