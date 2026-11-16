---
status: draft
issue: 2037
spec: spec/2026-11-16-2037-spec-job-followups.md
coders:
  1: lead   # tightly coupled with step 2 in one function
  2: lead   # same reason; one context
  3: lead   # one test file, depends on 1 and 2
reviewers:
  - codex    # independent read of the fetch-back logic
  - agy      # second model, different blind spots
---

# Plan: spec Job follow-ups

Branch `fix/2037-spec-job-followups` from `origin/dev`. Decisions are those of
the approved spec: strip Job status keys and carry the local ones over; fail
closed on a non-missing plan read error; local poller deadline 7500 s; cancel
event for the fetch-back; truncate and drop log lines; chmod 0644; tests.

## Steps

1. `apps/backend/core/workspace_fetch.py` (`_checked_spec_json`, `fetch_spec_files`,
   ~750-870): strip keys, carry local ones, `failed` out-list, `cancel` event,
   chmod 0644 → `pytest tests/test_spec_job_backend.py -k fetch`.
   Traps: vendored `artifact_store.py` stays untouched; ruff plain imports.
2. `apps/web-server/server/services/spec_job_backend.py` (`_finish_spec_fetch`,
   `SpecJobProcess`, `dispatch`): fail closed on `failed`, deadline (30 s bound per API call), cancel on
   stop, log cap → `pytest tests/test_spec_job_backend.py`.
3. `tests/test_spec_job_backend.py`: cases for each fix plus the two gaps →
   `pytest tests/test_spec_job_backend.py`, `ruff check`, `ruff format --check`.

## Tests

`pytest tests/test_spec_job_backend.py tests/test_issue_1771_monitor_raise_terminal.py`
passes; ruff clean.

## Rollback

Revert the PR. The flag is off, so nothing live depends on it.
