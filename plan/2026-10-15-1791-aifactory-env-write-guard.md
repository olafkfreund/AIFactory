---
status: draft
issue: 1791
spec: spec/2026-10-15-1791-aifactory-env-write-guard.md
---

# Plan: refuse agent writes to `.aifactory/.env` on the Claude SDK path

Approved decisions (from the spec):

- A file-tool write whose lexical (`normpath`) or resolved (`resolve()`)
  target ends in the components `.aifactory`, `.env` (exact case) is
  refused. Fail closed on resolve errors. Fixed message, no path in it or in
  the log.
- Wired into the existing `git_dir_write_hook` loop (matcher
  `Write|Edit|MultiEdit|NotebookEdit` unchanged) and into the two
  `ToolExecutor` write sites.
- Worktree grant: Read/Glob/Grep on `<main>/.aifactory/**` and
  `<main>/.worktrees/**` stay; Write/Edit only on
  `<main>/.aifactory/specs/**` and `<main>/.worktrees/**`.
- No new Bash heuristic (the `.git` guard has none); residual risk noted in
  the PR.

## Steps

1. `apps/backend/security/filesystem_validators.py` after line 198: add

   ```python
   def aifactory_env_write_refusal(path: str, cwd: str | None) -> str | None:
       """Refuse writes to a project settings file ``.aifactory/.env`` (#1791)."""
   ```

   Same structure as `git_metadata_write_refusal` (`:175-198`): expanduser,
   absolute against `cwd or Path.cwd()`, forms = normpath and resolve in a
   try that returns the refusal on `(OSError, RuntimeError, ValueError)`;
   refuse if any form has `f.parts[-2:] == (".aifactory", ".env")`.
   Message: `"Refused: agents may not write project settings (.aifactory/.env)."`
   → verify: `ruff check apps/backend/security/filesystem_validators.py`.
   Traps: case-sensitive on purpose; do not casefold.

2. `apps/backend/security/hooks.py:24` import the new function next to
   `git_metadata_write_refusal`; in `git_dir_write_hook` (`:261-283`) set
   `refusal = git_metadata_write_refusal(path, cwd) or aifactory_env_write_refusal(path, cwd)`,
   change the log line to `"Blocked agent write into a protected path"` and
   the docstring to mention `.aifactory/.env` (#1791). Keep the function
   name. `apps/backend/tools/executor.py:36` import it; at `:220` and
   `:243` append `or aifactory_env_write_refusal(file_path, str(self._working_dir))`.
   → verify: `pytest tests/test_command_sandbox.py -q` (existing tests still
   pass; `test_block_log_has_no_path` asserts "Blocked agent write").
   Traps: keep the log free of paths.

3. `apps/backend/core/client.py:711-723`: replace the single
   `permission_ops` loop with read ops `["Read", "Glob", "Grep"]` on both
   dirs, and write ops `["Write", "Edit"]` on
   `original_project_dir / ".aifactory" / "specs"` and
   `original_project_dir / ".worktrees"`, each only if it exists, using
   `str(dir_path.resolve())` as now. Add a one-line comment: no write grant
   on the rest of `.aifactory` (holds `.env`, #1791).
   → verify: step 4's settings test.
   Traps: do not touch the other allow rules; the existing absolute-path
   rule syntax stays as is.

4. `tests/test_command_sandbox.py`: add `class TestAifactoryEnvWriteGuard`
   after `TestGitDirWriteGuard`, reusing `_write_hook` and `_ti`. Fixture:
   `main/.aifactory/.env`, `main/.aifactory/specs/`, worktree dir
   `main/.aifactory/worktrees/tasks/t/` with a symlink `envlink` →
   `main/.aifactory/.env`. Blocked: Write/Edit/MultiEdit/NotebookEdit on the
   absolute path; `..` path relative to the worktree cwd; the symlink;
   `ToolExecutor(working_dir=main).execute("Write", {".aifactory/.env"})`
   leaves the file unchanged. Allowed: worktree file, `main/.aifactory/specs/x.md`,
   `main/.aifactory/.env.example`, `main/app/.env`, `main/.AIFACTORY/.env`.
   Plus `test_worktree_grant_narrowed`: monkeypatch `HOME` to tmp, patch
   `core.client.ClaudeSDKClient` and `core.client.require_auth_token` as in
   `tests/test_remote_control_wiring.py`, call `create_client` with the
   worktree as `project_dir` and a spec dir inside it, read
   `<HOME>/.claude/settings-headless.json`; assert no `Write(<main>/.aifactory/**)`
   / `Edit(...)`, but `Read(<main>/.aifactory/**)` and
   `Write(<main>/.aifactory/specs/**)` present.
   → verify: `pytest tests/test_command_sandbox.py -q -k "AifactoryEnv or narrowed"`
   passes; same tests fail with `origin/dev` copies of the three source files.
   Traps: never write the real `~/.claude`; no exploit text in test names
   or docstrings.

## Tests

```
cd <worktree>; PATH=/mnt/code/Source-home/GitHub/AIFactory/apps/backend/.venv/bin:$PATH
pytest tests/test_command_sandbox.py tests/test_remote_control_wiring.py -q
ruff check <touched files>; ruff format --check <touched files>
```
Expected: all pass; new tests fail on dev.

## Rollback

Revert the PR. No data or config migration.
