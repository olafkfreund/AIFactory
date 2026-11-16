---
status: draft
issue: 2036
spec: spec/2026-11-16-2036-github-env-test-isolation.md
coders:
  1: lead   # single small edit of one test file; handoff costs more than it saves
reviewers:
  - codex
  - agy
---

# Plan: isolate test_github_env.py

Decisions carried from the spec: stub gh first on PATH reading hosts.yml;
HOME/XDG_CONFIG_HOME in tmp; GIT_CONFIG_GLOBAL=/dev/null unless the test supplies
its own file; GIT_CONFIG_NOSYSTEM=1; assertions never print the token.

## Steps

1. tests/test_github_env.py: add `_stub_gh(tmp_path)`, `_isolate(env, tmp_path)`
   and `_redact(s)` near `_mode`; use them in the two tests; replace
   `assert TOKEN in fill.stdout` and `assert r.stdout.strip() == TOKEN` with
   boolean-compared, redacted messages → verify by
   `pytest tests/test_github_env.py` and a deliberate-failure run.
   Traps: ruff sorting (plain imports); no real gh on the path.

## Tests

`cd apps/backend && pytest ../../tests/test_github_env.py` (use repo's command);
ruff check/format on the file.

## Rollback

Revert the commit; test-only change.

## Review notes (Codex; agy returned empty output at 300s)

- Adopted: run the git/gh subprocesses with `cwd=tmp_path` so no repository
  `.git/config` is read. Deviation recorded: step 1 also adds `cwd=tmp_path`.
- Adopted: `_isolate` keeps a `gitconfig` parameter (store test needs its own file).
- Not adopted: the store test passing even without `github_env`'s own reset
  (`ce.child_env()` also resets) is pre-existing and out of scope for #2036.
