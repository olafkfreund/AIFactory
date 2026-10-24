---
status: approved
issue: Factory#813
spec: spec/2026-10-24-813-aif-codeql-hygiene.md
---

# Plan: clear the four real AIFactory CodeQL hygiene alerts

Branch `fix/813-aif-codeql-hygiene`, base `dev`, one PR into `dev`. The work is
three files plus one test, so it stays with the session model and is not
handed to `coder`. Line numbers are from `origin/dev` at 2a9e9ea2.

Approved decisions:

- 2431/2432: wrap every logged argument of `_refresh_to_remote_base` in the
  existing `sanitize_log()`. No wording or log-level change.
- 2423: delete the no-op self-assignment.
- 1621: initialise `msg_type` to `None` **before** `try:` and keep the real
  assignment **inside** the `try`. Do not move the assignment out, because a
  raising `type` attribute would then crash the stream.
- One new test, for 1621 only. The other two are mechanical and existing tests
  cover the code paths.

## Steps

1. `apps/web-server/server/services/build_backend.py:1035-1050`
   (`_refresh_to_remote_base`). Make these changes:
   - In `_log.warning` (1035-1041): `base_branch` becomes `sanitize_log(base_branch)`,
     `wt_path` becomes `sanitize_log(wt_path)`, and `fetch.stderr.strip() or "no stderr"`
     becomes `sanitize_log(fetch.stderr.strip() or "no stderr")`.
   - In `_log.info` (1045-1050): `wt_path` becomes `sanitize_log(wt_path)` and
     `base_branch` becomes `sanitize_log(base_branch)`.

   Check: `grep -n "sanitize_log" apps/web-server/server/services/build_backend.py | sed -n '1,20p'`
   shows the five new calls, and
   `pytest tests/test_build_backend_kubejob.py -k "refresh or unreachable" -q`
   passes.
   Traps: `sanitize_log` is already imported at line 105, so do not add a
   second import. Do not touch the `_git([...])` argv calls, because only log
   arguments are sinks. Keep the format string unchanged.

2. `apps/web-server/server/services/completion_orchestration.py:166`: delete the
   line `terminal_status = terminal_status` and nothing else.

   Check: `grep -n "terminal_status = terminal_status" apps/web-server/server/services/completion_orchestration.py`
   returns nothing, and
   `pytest tests/test_completion_marker_means_delivered.py tests/test_issue_1771_monitor_raise_terminal.py tests/test_failed_build_reaches_the_task.py -q`
   passes.
   Traps: `terminal_status` is a parameter (line 91), may be reassigned at line
   135, and is used at 172 and 579. Do not rename it or remove any other use.

3. `apps/backend/core/enforcement.py:609-612` (`_WrappedResponseStream._capture`):
   insert `msg_type: Any = None` as the first statement of the method body, after
   the docstring and before `try:`. Leave line 612 as it is.

   Check: `ruff check apps/backend/core/enforcement.py` is clean.
   Traps: `Any` is already imported (line 32). Do not move line 612 out of the
   `try`. Keep the `except Exception` and `logger.debug` unchanged.

4. `tests/claude_enforcement/test_wrapper_streaming.py`: append
   `test_capture_swallows_error_from_message_type`. Follow the existing pattern
   (`ClaudeUsageSnapshot`, `_Parent`, `_WrappedResponseStream(inner=None, usage=..., parent=...)`).
   Pass a message class whose `type` is a `@property` that raises
   `RuntimeError`, and assert that `wrapped._capture(msg)` returns without
   raising (just call it, since any exception fails the test).

   Check: the test fails on the unmodified file with `UnboundLocalError` (run
   it once with step 3 stashed out by hand, or before step 3), and passes after
   step 3: `pytest tests/claude_enforcement -q`.
   Traps: the file puts `apps/web-server` and `apps/backend` on `sys.path` itself,
   so add no conftest changes. Never use `git stash` in this worktree. To see the
   red run, write the test before step 3.

## Tests

- `pytest tests/claude_enforcement tests/test_build_backend_kubejob.py tests/test_completion_marker_means_delivered.py tests/test_issue_1771_monitor_raise_terminal.py tests/test_failed_build_reaches_the_task.py -q`
  is all green.
- `ruff check` and `ruff format --check` pass on the three source files and the test.
- The pre-commit hooks pass (they can take 10+ minutes, so use a long timeout
  and never `--no-verify` on the code commit).
- After merge, CodeQL on `dev` shows 2431, 2432, 2423 and 1621 as fixed.

## Rollback

`git revert` the squash-merge commit on `dev`. Every change is local and
independent, and none touches data or config.
