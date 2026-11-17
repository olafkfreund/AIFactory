---
status: approved
issue: 2101
spec: spec/2026-11-17-2101-marker-nonce-settings-isolation.md
coders:
  1: lead
reviewers:
  - codex
---

# Plan: isolate project settings in the dispatch test

Decisions: fix the test, not the code; use the sibling pattern of
stubbing `project_settings_store.get_settings` to return `{}`; no skip/xfail.

## Steps

1. `tests/test_marker_nonce.py` `test_kubejob_dispatch_puts_the_nonce_in_the_job_env`
   (~line 243): add an async `_no_settings` returning `{}` and
   `monkeypatch.setattr("server.services.project_settings_store.get_settings", _no_settings)`
   with a one-line comment citing #2101. The stub must accept `*_args, **_kwargs` (it is called with project_path and session_factory).
   → verify: `apps/backend/.venv/bin/pytest tests/test_marker_nonce.py -q` (green;
   it was red before), then ruff check + format --check on the file.
   Traps: keep imports in the existing style; ruff alias-sorting quirk does not apply.

## Tests

`pytest tests/test_marker_nonce.py tests/test_build_backend_kubejob.py -q`.

## Rollback

Revert the commit.
