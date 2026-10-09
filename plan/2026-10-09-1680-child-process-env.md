---
status: approved
issue: 1680
spec: spec/2026-10-09-1680-child-process-env.md
---

# Plan: child processes must not carry the web server's secrets

## Approved decisions (self-contained summary)

1. **One helper.** `child_env(keep=(), extra=None)` in
   `apps/web-server/server/utils/subprocess_env.py` returns a copy of
   `os.environ` minus every key `core.auth.is_denied_env_key` matches and
   minus `_STRIP_VARS`. It then puts back the `keep` names that are present
   and applies `extra`. Finally it appends `core.hooksPath=/dev/null` as git
   env config at index `GIT_CONFIG_COUNT` and raises the count by one. An
   existing entry 0 (the production `credential.https://github.com.helper`)
   is preserved.
2. **One deny list.** A new `core.auth.is_denied_env_key(name) -> bool`
   wraps `_AGENT_ENV_DENY_EXACT` and `_AGENT_ENV_DENY_PATTERN`.
   `get_agent_env_blanks` uses it and keeps its `_AGENT_ENV_KEEP` and
   API-key-auth handling unchanged.
3. **Keep-sets.**
   - `GITHUB_KEEP = ("GITHUB_TOKEN", "GH_TOKEN")`.
   - `RUNNER_KEEP` is `GITHUB_KEEP` plus `OPENAI_API_KEY`,
     `OPENAI_COMPATIBLE_API_KEY`, `GEMINI_API_KEY`, `GOOGLE_API_KEY`,
     `OPENROUTER_API_KEY` and `VOYAGE_API_KEY`.
   - `make_subprocess_env(extra, *, strip_anthropic_api_key=True)` becomes
     `child_env(keep=RUNNER_KEEP + (("ANTHROPIC_API_KEY",) if not
     strip_anthropic_api_key else ()), extra=extra)`. Its signature and
     callers are unchanged.
4. **Who gets what.**
   - `gh`, and git that pushes or fetches: `child_env(keep=GITHUB_KEEP)`.
   - Local git and tools: `child_env()`.
   - LLM runners: `make_subprocess_env`.
   - Provider CLIs: claude gets `keep=("CLAUDE_CODE_OAUTH_TOKEN",)` and
     antigravity the same. Codex gets `("CLAUDE_CODE_OAUTH_TOKEN",
     "OPENAI_API_KEY")`, because the audit marks `OPENAI_API_KEY` optional
     for codex and dropping it would break API-key codex users. This
     refines the spec's single keep-set for the provider CLIs.
   - **Exempt:** `pty/session.py` only.
5. **Runner hardening.** A new `apps/backend/core/process_hardening.py`
   defines `make_non_dumpable() -> bool`. On Linux it calls
   `prctl(PR_SET_DUMPABLE, 0)` and returns success; elsewhere it is a no-op
   that returns True.
   - `core/client.create_client` and
     `core/simple_client.create_simple_client` call it before spawning an
     agent. On False they log a warning and continue.
   - The web server's `_make_non_dumpable` (`server/main.py:754`) calls it
     and keeps its fail-closed `SystemExit` on False.
6. **Enforcement.** Unit tests, an AST scan test with the PTY session as the
   one allow-listed site, and a runner-hardening test.
7. **Follow-ups to file:**
   - a per-call askpass for `GITHUB_TOKEN`, so it leaves the `gh`/`git push`
     environment;
   - pinning `core.fsmonitor=false`, and auditing agent writes to
     `.git/config`.

## Repo traps (every step)

- **Venv:**
  - `V=/mnt/code/Source-home/GitHub/AIFactory/apps/backend/.venv`
  - Root tests: `$V/bin/python -m pytest tests/...`
  - Web-server tests: `$V/bin/python -m pytest apps/web-server/tests ... -o asyncio_mode=auto`
  - Plain `pytest` in `apps/backend` misses `sys.path`.
- **Ratchets after `git add`:** both must report 0 regressed, and that
  includes test files.
  - `$V/bin/python scripts/cq_ratchet.py --tool ruff --base origin/dev --ruff $V/bin/ruff --config standards/ruff.toml --staged --paths 'apps/backend/*.py' 'apps/web-server/*.py' 'scripts/*.py'`
  - The same with `--tool mypy --mypy $V/bin/mypy --config standards/mypy.ini`.
  - New files must be clean under strict ruff and mypy: no bare `dict`, no
    function-level imports without `# noqa: PLC0415`, at most 5 arguments
    and 6 returns.
- **Autonomy matrix:** editing `core/auth.py` or `server/main.py` shifts the
  line numbers it embeds. Run `$V/bin/python scripts/gen_autonomy_matrix.py`,
  then `--check`, and commit the two regenerated files with the step.
- **Commits:**
  - The session model commits; the coder cannot.
  - Messages look like `fix(security): <what> (#1680, plan step N)`, with a
    subject of at most 100 characters and the session trailers at the end.
- **Build the env per call.** Never cache `child_env()` at import time:
  `os.environ` changes at runtime (`.env` bootstrap, `gh auth`).
- **Aliased imports.** `completion_orchestration.py` imports subprocess as
  `_sp`, so the scan must resolve import aliases.
- **`GIT_CONFIG_COUNT` may be absent or non-numeric.** Treat a bad value
  as 0 and overwrite entries from 0: git would reject a bad count anyway.
- **Don't change argv or behaviour,** only `env=`. Where a site already
  builds a custom env, merge it: `child_env(keep=..., extra={...})`.
- **The plan is the contract.** A deviation updates this file in the same
  commit.

## Steps

1. **Failing tests first.**
   - **`tests/test_child_process_env.py`:**
     - `child_env()` drops `DATABASE_URL`, `JWT_SECRET`, `API_TOKEN`,
       `AIFACTORY_TOKEN`, `AIFACTORY_TRUSTED_PLAN_KEY_PFACTORY`,
       `S3_SECRET_KEY`, `APP_OIDC_CLIENT_SECRET` and `GITHUB_TOKEN`
       (set them with monkeypatch);
     - it keeps `PATH` and `HOME`;
     - `keep=GITHUB_KEEP` restores `GITHUB_TOKEN` and not `DATABASE_URL`;
     - with `GIT_CONFIG_COUNT=1` and entry 0 set, the result has
       `GIT_CONFIG_COUNT=2`, entry 0 unchanged, and entry 1 is
       `core.hooksPath`/`/dev/null`;
     - with no count set, entry 0 is the hooks entry;
     - `make_subprocess_env()` keeps `OPENAI_API_KEY` and `GITHUB_TOKEN`,
       drops `DATABASE_URL` and `ANTHROPIC_API_KEY`, and keeps
       `ANTHROPIC_API_KEY` when `strip_anthropic_api_key=False`;
     - **real git:** in a `tmp_path` repo with an executable
       `.git/hooks/pre-commit` that exits 1,
       `git commit --allow-empty -m x` under `env=child_env()` returns 0,
       and under `os.environ` returns non-zero.
   - **`tests/test_no_unscrubbed_spawn.py`:**
     - An AST walk of `apps/web-server/server/**/*.py` resolves the
       aliases of `subprocess` and `asyncio` and finds calls to `run`,
       `Popen`, `check_output`, `check_call`, `create_subprocess_exec` and
       `create_subprocess_shell`.
     - It fails if a call has no `env=` keyword, or if the keyword's value
       is `os.environ`, `os.environ.copy()`, or a dict display that
       unpacks `**os.environ`.
     - Allow-list: `apps/web-server/server/pty/session.py`, with a comment
       giving the reason.
     - The test prints every offending `file:line`.
   - **`tests/test_runner_hardening.py`:**
     - Patch `core.process_hardening.make_non_dumpable` to raise a
       sentinel. `create_client(...)` and `create_simple_client(...)` each
       raise it; build the minimal args the way
       `tests/test_agent_env_scrub.py` does.
     - On Linux, a subprocess that calls `make_non_dumpable()` reports
       `prctl(PR_GET_DUMPABLE)` as 0.
   - Verify: all three files fail for the expected reasons (missing
     functions, offending sites listed). Commit as red.
2. **`core/auth.py` and `core/process_hardening.py`.**
   - Add `is_denied_env_key`, and rewrite the deny check in
     `get_agent_env_blanks` to use it, with behaviour unchanged.
   - Create `process_hardening.py` with `make_non_dumpable()` (ctypes
     `prctl`, option 4, arg 0; non-Linux returns True).
   - Make `server/main.py:_make_non_dumpable` call it:
     `if not make_non_dumpable(): raise SystemExit(...)`. Keep the existing
     server tests green by patching the new location if needed.
   - Regenerate the matrix.
   - Verify: `tests/test_agent_env_scrub.py`,
     `apps/web-server/tests/test_server_non_dumpable.py` and the runner
     `prctl` test pass.
3. **`subprocess_env.py`.**
   - Add `child_env`, `GITHUB_KEEP` and `RUNNER_KEEP`, and re-base
     `make_subprocess_env` on them. Keep `_inject_traceparent` behaviour:
     apply it in `child_env`.
   - Verify: `tests/test_child_process_env.py` is fully green, and the
     existing `make_subprocess_env` tests (grep `tests/` and
     `apps/web-server/tests`) pass.
4. **Helpers get `env=`** (spec section 2), with no other change:
   - `pr_endgame._default_runner` (`GITHUB_KEEP`)
   - `services/gh.run_gh_command` (`GITHUB_KEEP`)
   - `routes/git.run_gh_command` (`GITHUB_KEEP`)
   - `routes/git.run_git_command` (none)
   - `pr_data_service._run_gh` (`GITHUB_KEEP`)
   - `TerminalWorktreeService._run_git_command` (none)
   - `task_branch._git` (none)
   - `build_backend._git` and `_git_out` (`GITHUB_KEEP`, extra
     `GIT_TERMINAL_PROMPT=0`)
   - `project_workspace_service._run_git` (none, extra
     `GIT_ALLOW_PROTOCOL` and `cred_env`)
   - `settings._run` (none)
   - `routes/git._run`, `routes/github._run` and
     `cli_accounts._run_login_shell` (none)
   - `copilot_dispatch` (`GITHUB_KEEP`)

   Verify: the scan test's offending list shrinks to direct sites only, and
   the existing tests for each touched module pass.
5. **Direct sites.**
   - The 54 sites in `worktree_merge.py` (`child_env()`).
   - `pr.py`: the fetch and push at about :199, :296 and :326, and
     `gh auth setup-git` at :277, use `GITHUB_KEEP`; the rest get none.
   - `completion_orchestration.py`: the `_sp.run` `gh auth setup-git` and
     `git push` at about :398 (`GITHUB_KEEP`).
   - The provider CLIs (claude, codex, antigravity), per decision 4.
   - `ollama_provider`, `projects`, `changelog`, `context`, `files`,
     `github` (:430, the install script, and :620, `gh auth login`, which
     gets `GITHUB_KEEP`), `config` (openssl), `sandbox` (the bwrap probe),
     `tools` (rg), `worktree_tools` (the IDE Popen), `rmux/wrapper`, and
     the claude/CLI version probes.

   Verify: `tests/test_no_unscrubbed_spawn.py` is green, the full
   web-server suite passes, and the root suites for `worktree_merge`,
   `pr`, `projects` and `changelog` pass.
6. **Runner hardening.** Call `make_non_dumpable()` at the top of
   `create_client` and `create_simple_client`, logging a warning on False.
   Verify: `tests/test_runner_hardening.py` is green, and
   `tests/test_agent_env_scrub.py` and `tests/test_simple_client*` pass.
7. **CHANGELOG and follow-ups.**
   - Add a `### Security` entry under `## [Unreleased]`.
   - File the two follow-up issues (decision 7) and link them in the
     CHANGELOG entry and the PR.
   - Verify: the full root suite (`$V/bin/python -m pytest tests -q`) and
     the full web-server suite pass. Run the mutation checks below.

## Tests

- New: `tests/test_child_process_env.py`, `tests/test_no_unscrubbed_spawn.py`
  and `tests/test_runner_hardening.py`.
- **Mutation checks:**
  - remove `env=` from `routes/git.run_git_command`: the scan fails;
  - drop the hooksPath append: the real-git hook test fails;
  - drop the `make_non_dumpable()` call in `create_client`: the runner test
    fails.
- **After deploy, in the pod:**
  - while a build runs, `cat /proc/<run.py pid>/environ` from a same-uid
    `kubectl exec` shell is denied;
  - a task's PR endgame still opens its PR (`gh` and `git push` work).

## Rollback

Revert the squash-merge commit. There are no schema or data changes. Kubejob
builds are untouched, so a revert affects only in-pod spawns.

## Deviations

- **Step 2:** `server/main.py` `_make_non_dumpable` puts `apps/backend` on
  `sys.path` and imports `core.process_hardening` inside the function
  (`# noqa: PLC0415`), the pattern `routes/execution.py` and others already
  use; the server's startup path does not guarantee `core` is importable. The
  SystemExit message drops the errno. `test_hardening_fails_closed` now
  patches `core.process_hardening.ctypes.CDLL`. The ratchet commands need
  `--staged` to see staged files.
- **Step 4:** network git needs the GitHub token for gh's credential helper,
  so `task_branch._git` (fetch), `routes/git.run_git_command` (fetch, pull)
  and `project_workspace_service._run_git` (clone and fetch when no stored
  credential) use `child_env(keep=GITHUB_KEEP)` rather than the spec's
  `child_env()`. The scan missed envs built from `os.environ` into a
  variable first (`build_backend._git`, `_run_git`), so
  `test_no_environ_copy_outside_helper` now flags any whole-environ copy
  outside `subprocess_env.py` and the PTY session.
- **Step 5:** antigravity also keeps `GEMINI_API_KEY` and `GOOGLE_API_KEY`
  (`cli_accounts` detects Gemini API-key auth), alongside the OAuth token.
  `test_resolve_conflicts_reads_the_commit` forced a failing commit with a
  pre-commit hook, which hooksPath now disables; it fails the commit via
  `commit.gpgsign` and `gpg.program=false` instead. Consequence of the plan:
  a repo's own hooks no longer run on server-made merge and conflict commits.
- **Step 8 (review fix):** the server runs backend code in-process that
  spawned git with the full environment (`authed_push_url` built its env from
  `os.environ`, reached via the TFactory auto-handoff push; `workspace_fetch`,
  `tfactory_client`, `cli.workspace_commands`, `gate_runner`, `trusted_plan`
  and the macOS keychain call likewise). The scrub core moved to
  `apps/backend/core/child_env.py`, which the server's `subprocess_env`
  wraps; every spawn reachable from the server is scrubbed, and the scan
  covers the backend files the server runs in-process (`cli/workspace_commands`
  and `gate_runner` are left out: they keep runner-only sites that run a
  project's own gates). `authed_push_url` no longer passes `GITHUB_TOKEN`;
  the askpass pair carries it.
- **Hooks in builds (user decision, 2026-10-09):** `make_subprocess_env`
  carries `core.hooksPath=/dev/null` to `run.py` and the agent, so a
  project's own hooks no longer run in builds either. Intended.
- **Git LFS (user decision):** with hooks off, LFS objects are not uploaded
  on push. Known limit, follow-up #1690.
