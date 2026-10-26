---
status: approved
issue: 1900
spec: spec/2026-10-26-1900-from-issue-repo-binding.md
---

# Plan: bind a from-issue declared repo to the project's own repository

Decisions (from the spec):

- A declared `repo` must match the project's own repo: `settings.gitRepo`, `clonedFrom`, or the checkout's `origin`, normalised and compared case-insensitively. Otherwise 400, before the fetch, the dedup lookup or any write.
- An explicit `gitProvider` that contradicts the declared host gives 400. An unset one keeps the RFC-0020 behaviour.
- With no declared repo, nothing changes. A declared repo for a project with no known repo gives 400 (fail closed).
- The check lives in `from_issue.py`, not in the shared `_get_project_provider`.
- The implementer is Claude: two source/test files, under the handoff threshold.

## Steps

1. `apps/web-server/server/routes/from_issue.py`: add `_repo_path`, `_project_repos` and `_check_declared_repo` next to `_declared_repo` (~line 115). Call the check in `create_from_issue` right after the 404 project check (~line 300).
   → verify by `pytest tests/test_from_issue_route.py -q`
   Traps: use absolute `server.` imports (TID252 ratchet); the subprocess call needs a list argv, a timeout and no shell; keep the strict-ruff ratchet at or below base (`scripts/cq_ratchet.py`).
2. `tests/test_from_issue_route.py`: the `project` fixture binds project `p` to the repos its tests declare (`platform/pipelines`, `a/x`, `b/y`). Add `tests/test_from_issue_repo_binding.py` with the spec's cases.
   → verify by `pytest tests/test_from_issue_repo_binding.py tests/test_from_issue_route.py -q`

## Tests

The two files above, plus `scripts/cq_ratchet.py` (ruff and mypy) against `origin/dev`, and `ruff format --check`.

## Rollback

Revert the PR; a declared repo goes back to overriding `gitRepo`.
