---
status: approved
issue: 1931
spec: spec/2026-11-16-1931-env-only-secrets-transcript-key.md
coders:
  1: coder
  2: codex
reviewers:
  - codex
---

# Plan: env-only secrets switch and project-qualified transcript key

Decisions carried from the spec: opt-in `APP_REQUIRE_ENV_SECRETS` (default off, raise naming the env var); transcript key uses `JOB_ID`, control plane lists by `task_id`; no new env contract; subprocess backend keeps spec-id key; run id stays out of scope.

## Steps

1. `apps/web-server/server/config.py` (field list near line 132, `__init__` ~281): add `REQUIRE_ENV_SECRETS`; raise `ValueError` if on and `API_TOKEN`/`JWT_SECRET` empty. Add tests in `apps/web-server/tests/test_config_env_secrets.py` → `pytest apps/web-server/tests/test_config_env_secrets.py`
   Traps: use plain-name imports (ruff sorting); never print values.
2. `apps/backend/core/workspace_fetch.py` (`maybe_push_transcripts`, `transcript_uris` ~369-415), `apps/web-server/server/services/completion.py` (~1182, pass `task_id`), `apps/web-server/server/routes/tasks_regression.py` (~27, allow `:` in the URI regex, keep the other validation). Update `apps/backend/core/test_workspace_fetch_transcript.py`, the transcript mocks in `apps/web-server/tests/test_completion_emitter.py` (~1608-1630: accept `job_id`, assert it receives `proj:s`) and `test_post_merge_regression_route.py` → `pytest apps/backend/core/test_workspace_fetch_transcript.py apps/web-server/tests/test_completion_emitter.py apps/web-server/tests/test_post_merge_regression_route.py`
   Traps: `workspace_fetch.py` is not vendored; check `git grep -l workspace_fetch` for byte-checked copies before editing.

## Tests

The pytest commands above plus `ruff check` and `ruff format --check` on touched files.

## Rollback

Revert the PR; the switch defaults off and the key change only affects new uploads.
