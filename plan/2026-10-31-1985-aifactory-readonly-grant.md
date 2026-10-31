---
status: approved
issue: 1985
spec: spec/2026-10-31-1985-aifactory-readonly-grant.md
coders:
  1: codex  # lane brief names codex for this wave
  2: codex
---

# Plan: agents get read-only access to the source project's .aifactory/

Approved decisions (from the spec):
- Grant on `<source>/.aifactory/**` and legacy `<source>/.worktrees/**` in a
  worktree session becomes `Read/Glob/Grep`.
- Enforcement lives in the existing `git_dir_write_hook` via
  `protected_write_refusal`: from a task worktree
  (`/.aifactory/worktrees/tasks/`) or PR-review worktree
  (`/.aifactory/github/pr/worktrees/`) cwd, refuse a file-tool write whose
  lexical (normpath) or resolved form is under `<source>/.aifactory` and not
  under the worktree (the cwd). Fail closed on resolution errors. No exception
  for `spec_dir`; no per-file grant (no agent-side writer exists).
- Bash leg out of scope (#1932).

## Steps

1. `apps/backend/security/filesystem_validators.py` (after
   `git_metadata_write_refusal`, ~170-200): add
   `source_aifactory_write_refusal(path: str, cwd: str | None) -> str | None`
   as above (returns None when cwd is falsy or not a worktree), and call it
   from `protected_write_refusal` after the git check. Update the
   `git_dir_write_hook` docstring in `apps/backend/security/hooks.py` (~277)
   to mention #1985. In `apps/backend/core/client.py` ~770 set
   `permission_ops = ["Read", "Glob", "Grep"]` with a one-line why comment.
   → verify by `ruff check` and `ruff format --check` on the three files.
   Traps: keep the function pure-Path (no new imports beyond os/pathlib);
   Linux-only paths like the sibling functions.
2. `tests/test_source_aifactory_readonly.py` (new): tmp tree
   `proj/.aifactory/specs/001/`, worktree `proj/.aifactory/worktrees/tasks/001/`
   with `.aifactory/specs/001/`. Through `git_dir_write_hook` with cwd=worktree:
   Write and Edit blocked for the source `review_state.json` (absolute,
   relative `../../../specs/001/review_state.json`, symlink in the worktree);
   allowed for `src/a.py` and the localized `implementation_plan.json`; a
   PR-review worktree cwd behaves the same; cwd=proj leaves the source spec dir
   writable. `create_client` with project_dir=worktree (patch
   `core.client.ClaudeSDKClient` and `core.client.require_auth_token` as in
   `tests/test_remote_control_wiring.py`; monkeypatch HOME to tmp so the
   settings file is not written to the real home): the settings JSON allow list
   has `Read(<source>/.aifactory/**)` and no `Write(`/`Edit(` for it.
   → verify by `apps/backend/.venv/bin/pytest tests/test_source_aifactory_readonly.py tests/test_profile_untrusted.py tests/test_command_sandbox.py -q`
   passing, and the new test failing with the step-1 changes stashed out
   (revert files locally, not git stash).
   Traps: tests import backend via sys.path like the sibling tests.

## Tests

`apps/backend/.venv/bin/pytest tests/test_source_aifactory_readonly.py tests/test_profile_untrusted.py tests/test_command_sandbox.py tests/test_remote_control_wiring.py -q`
all pass; `python scripts/ratchet_lint.py` if present passes.

## Rollback

Revert the PR commit. No data or config migration.
