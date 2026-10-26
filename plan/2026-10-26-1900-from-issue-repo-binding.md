---
status: approved
issue: 1900
spec: spec/2026-10-26-1900-from-issue-repo-binding.md
---

# Plan: bind a from-issue declared repo to the project's own repository

Decisions (from the spec):

- A declared `repo` must match the project's own repo: `settings.gitRepo`, `clonedFrom`, or the checkout's `origin`. Identity is (host, path), normalised and lowercased. A declared host must be one of the project's hosts (theirs plus the `gitBaseUrl` host); a bare `owner/repo` is compared by path. Otherwise 400, before the fetch, the dedup lookup or any write. (Revised after the review of #1901.)
- The declared provider prefix must equal the project's provider: `gitProvider`, else derived from the own host (`github.com`, `gitlab.com`, `dev.azure.com`), else `github` when nothing is hosted. A self-hosted host with `gitProvider` unset derives nothing.
- Azure forms (`ssh.dev.azure.com:v3/`, `org.visualstudio.com/proj/_git/repo`, `/_git/`) normalise to `org/proj/repo` on `dev.azure.com`. A `gitBaseUrl` subpath is stripped from URLs on that host (self-hosted GitLab).
- With no declared repo, nothing changes. A declared repo for a project with no known repo gives 400 (fail closed).
- The check lives in `from_issue.py`, not in the shared `_get_project_provider`.
- The implementer is Claude: two source/test files, under the handoff threshold.

## Steps

1. `apps/web-server/server/routes/from_issue.py`: add `_repo_path`, `_project_repos` and `_check_declared_repo` next to `_declared_repo` (~line 115). Call the check in `create_from_issue` right after the 404 project check (~line 300).
   → verify by `pytest tests/test_from_issue_route.py -q`
   Traps: use absolute `server.` imports (TID252 ratchet); the subprocess call needs a list argv, a timeout and no shell; keep the strict-ruff ratchet at or below base (`scripts/cq_ratchet.py`).
2. `tests/test_from_issue_route.py`: the `project` fixture binds project `p` to the repos its tests declare (`platform/pipelines`, `a/x`, `b/y`). Add `tests/test_from_issue_repo_binding.py` with the spec's cases.
   → verify by `pytest tests/test_from_issue_repo_binding.py tests/test_from_issue_route.py -q`

3. (Revision) `routes/from_issue.py`: replace `_repo_path` with `_repo_id(value, base) -> (host, path)`; `_project_repos` returns `_OwnRepo(provider, paths, hosts, base)`; `_check_declared_repo` compares path, host and provider as above. Update the `test_from_issue_route.py` fixture to return an `_OwnRepo`. Rewrite `tests/test_from_issue_repo_binding.py` with the spec's revised cases, and mock `_fetch_issue_via_provider` in every test, including the no-known-repo case.
   → verify by `pytest tests/test_from_issue_repo_binding.py tests/test_from_issue_route.py -q` and both `scripts/cq_ratchet.py` runs.

## Tests

The two files above, plus `scripts/cq_ratchet.py` (ruff and mypy) against `origin/dev`, and `ruff format --check`.

## Rollback

Revert the PR; a declared repo goes back to overriding `gitRepo`.

## Deviations

- Step 1: the `origin` read reuses `server.routes.git.run_git_command` instead of a new `subprocess.run`, which the strict ratchet flagged (S603/S607). `project_registry` is imported at module level (PLC0415) and called as `project_registry.load_projects()`, so tests can still patch it.
- Step 3, second review of #1901 (code within the approved spec):
  - When the provider cannot be derived (gitProvider unset, only self-hosted hosts), only `github` or no prefix is accepted. A `gitlab:` or `azure_devops:` prefix then needs `gitBaseUrl`, so the token never defaults to gitlab.com or dev.azure.com.
  - The Azure ssh host is matched exactly (`vs-ssh.visualstudio.com`), so `vs-ssh.<other>` keeps its own host.
  - `_check_declared_repo` returns the normalised `[provider:]owner/repo`, and that, not the raw string, is what is fetched, stored and used for dedup and correlation. The path keeps its case (`_repo_id(..., fold=False)`): lowercasing would change CFactory correlation keys (#1775) and miss the dedup of already-stored specs.
  - The `test_from_issue_route.py` fixture gains a base host so its `gitlab:` declaration stays valid.
