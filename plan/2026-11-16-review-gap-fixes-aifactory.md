---
status: approved
issue: 2047
spec: spec/2026-11-16-review-gap-fixes-aifactory.md
reviewers:
  - codex   # independent read-only diff review
# coders: none, one small step done by the lead
---

# Plan: seed failover provider logins

Decisions: as in the spec.

## Steps
1. apps/web-server/server/services/build_backend.py `cred_files_for_models`: add enabled failover chain providers → `pytest tests/test_build_backend_kubejob.py tests/test_spec_job_backend.py`. Test: test_cred_files_include_enabled_failover_providers. Traps: ruff plain names.

## Tests
ruff check/format on touched files; the two test files above.

## Rollback
Revert the commit.
