---
status: approved
issue: 1680
intent: intent/2026-10-09-1680-child-process-env.md
---

# Spec: child processes must not carry the web server's secrets

## Design

Approved intent answers: a deny-list built on the existing agent scrub, the
secrets a child needs passed by name, `core.hooksPath=/dev/null` on every git
the server runs, and a pytest that enforces it.

### 1. One child environment helper

`apps/web-server/server/utils/subprocess_env.py` gets `child_env(keep=(),
extra=None)`, and `make_subprocess_env` becomes a thin wrapper over it. It
returns a copy of `os.environ` that:

- **drops every key the agent scrub denies.** It calls one new predicate,
  `core.auth.is_denied_env_key(name)`, that wraps `_AGENT_ENV_DENY_EXACT` and
  `_AGENT_ENV_DENY_PATTERN`. `get_agent_env_blanks` is rewritten on top of
  it, so there is still exactly one list. It also drops `_STRIP_VARS` (the
  Anthropic direct-API key, as today).
- **puts back only the names in `keep`,** and only if they are set in
  `os.environ`.
- **appends `core.hooksPath=/dev/null` to git's environment config:** it
  writes `GIT_CONFIG_KEY_<n>` and `GIT_CONFIG_VALUE_<n>` at
  `n = GIT_CONFIG_COUNT` and raises `GIT_CONFIG_COUNT` by one. Production
  already sets entry 0 (`credential.https://github.com.helper = !gh auth
  git-credential`), and that entry is preserved. Any git started with this
  environment runs no repository hooks; git 2.56 is in the image, and git
  has supported this since 2.31. A non-git child ignores these variables.

Two named keep-sets live next to the helper:

- `GITHUB_KEEP = ("GITHUB_TOKEN", "GH_TOKEN")`, for `gh`, and for git that
  pushes or fetches through gh's credential helper.
- `RUNNER_KEEP` holds what `run.py` and the other backend agent runners read
  (audit, 2026-10-09): `GITHUB_KEEP`, plus `OPENAI_API_KEY`,
  `OPENAI_COMPATIBLE_API_KEY`, `GEMINI_API_KEY`, `GOOGLE_API_KEY`,
  `OPENROUTER_API_KEY` and `VOYAGE_API_KEY`. It does **not** include
  `DATABASE_URL`, `JWT_SECRET`, `API_TOKEN`, `AIFACTORY_TOKEN` or
  `AIFACTORY_TRUSTED_PLAN_KEY_*`, none of which `run.py` reads.

### 2. Every spawn site uses it

The audit found about 130 spawn sites in `apps/web-server/server`. Most go
through about ten helpers, and each helper gets an `env=` argument:

- `pr_endgame._default_runner`, `gh.run_gh_command`,
  `routes/git.run_gh_command`, `pr_data_service._run_gh` and
  `copilot_dispatch` use `child_env(keep=GITHUB_KEEP)`.
- `routes/git.run_git_command`, `TerminalWorktreeService._run_git_command`,
  `task_branch._git`, `settings._run` and the login-shell `_run` helpers use
  `child_env()`.
- `build_backend._git` uses `child_env(keep=GITHUB_KEEP)` and adds its
  `GIT_TERMINAL_PROMPT`.
- `project_workspace_service._run_git` uses `child_env()` and adds its
  `GIT_ALLOW_PROTOCOL` and askpass `cred_env`.
- The LLM runners (`agent_service._spawn_task_execution`,
  `agent_credential`, `agent_spec_creation`, `pr_review_service`,
  `insights_service`, `clarification_service`, `changelog_service`) keep
  calling `make_subprocess_env`, which now means
  `child_env(keep=RUNNER_KEEP)`.
- The provider CLIs (claude, codex and antigravity) use
  `child_env(keep=("CLAUDE_CODE_OAUTH_TOKEN",))`. That name is not denied,
  so it is listed only to make the dependency visible.
- **Direct sites** get `env=child_env()`, or `child_env(keep=GITHUB_KEEP)`
  for network git and gh: `pr.py` fetch and push, the
  `completion_orchestration` `gh auth setup-git` and `git push`. The rest are
  `worktree_merge.py` (54 local git), the other `pr.py` sites, `projects`,
  `changelog`, `context`, `files`, `github`, and the probes and tools.

**One exemption:** `pty/session.py`, the user's own interactive terminal,
keeps the full environment. It is not started on an agent's behalf.

### 3. Backend agent runners make themselves non-dumpable

`run.py` and the other runner processes still hold `RUNNER_KEEP`, which
includes the GitHub token and provider keys, for their whole life. Their agent
is a same-uid child and could read them from `/proc/<runner>/environ`. A shared
`core.process_hardening.make_non_dumpable()` applies the 3.8.3 fix
(`prctl(PR_SET_DUMPABLE, 0)`). It is called in `core/client.create_client`
and `core/simple_client.create_simple_client`, before any agent is spawned, so
it covers `run.py` and every other runner, whatever its entry point. The web
server's `_make_non_dumpable` calls the same function. Unlike the server, a
runner only logs a warning on failure (on macOS or Windows it is a no-op): its
agent env is already scrubbed, and a dev host must not stop building.

### 4. Enforcement

- **`tests/test_child_process_env.py`, unit tests:** no denied key survives
  `child_env()`; `keep` restores only present names; the `core.hooksPath`
  entry is appended after an existing `GIT_CONFIG_COUNT=1` without
  disturbing entry 0; and a real `git commit` in a temp repo with a failing
  `pre-commit` hook succeeds under `child_env()`.
- **`tests/test_no_unscrubbed_spawn.py`, an AST scan of
  `apps/web-server/server`:** every call to
  `asyncio.create_subprocess_exec`, `create_subprocess_shell`,
  `subprocess.run`, `Popen`, `check_output` and `check_call` (aliased imports
  included) must pass `env=`, and that value must not be `os.environ`,
  `os.environ.copy()` or `{**os.environ, ...}`. The PTY session is the one
  allow-listed site, named by path.
- **Runner hardening:** a test asserts that `create_client` calls
  `make_non_dumpable`.

## Alternatives rejected

- **Allow-list per child.** The user chose the deny-list. An allow-list
  breaks every unlisted variable a tool needs (`PATH`, locale, proxy,
  `HOME`, `KUBERNETES_*`).
- **Purge secrets from `os.environ` at server start.** About a dozen modules
  read `DATABASE_URL`, the trusted-plan keys and others lazily from
  `os.environ` (`job_state_store.store_enabled`,
  `trusted_plan.load_keyring_from_env`, and more). Moving them all to a
  private store is a larger, riskier change than fixing the spawn sites.
- **Monkeypatching `subprocess` and `asyncio` defaults.** It is invisible at
  the call site, it breaks libraries that spawn their own children, and it
  can't be scoped.
- **`-c core.hooksPath=/dev/null` in each git argv.** There are about 120
  argv lists, and one forgotten flag reopens the hole. The environment
  entry rides along with the helper.
- **Running the server's git as a different uid.** It needs a second user
  and file ownership changes on the PVC. That is out of proportion.

## Risks

- **A child that needed a now-scrubbed secret breaks.** The audit lists what
  each one reads, and the keep-sets cover it. Any miss surfaces as an auth
  error from `gh`, a provider or git push, not silently. The tests run the
  real helpers.
- **Hooks a user relied on for server commits stop running.** The audit
  found none in the server.
- **`GITHUB_TOKEN` is still in the environment of `gh` and network git**
  while they run, so a same-uid agent racing `/proc` could catch it. The
  window is the life of one `gh` or `git push`, and the runners themselves
  are non-dumpable. A per-call askpass would close it entirely: follow-up.
- **Other repo-config execution paths remain.** `core.fsmonitor`,
  `core.sshCommand`, filter and diff drivers in an agent-written
  `.git/config` can still run commands when the server runs git there.
  `core.hooksPath` covers only hooks. Follow-up issue: pin
  `core.fsmonitor=false` the same way, and audit `.git/config` writes.
- **Kubejob builds are unchanged** (`build_backend.py:259-300` already uses
  an allow-list), and so is the production auto-merge path. In production,
  `gh` and `git push` still get `GITHUB_TOKEN` through `GITHUB_KEEP`.

## Verification

- Both new tests pass, and the full web-server and root suites pass.
- **Mutation checks:** removing `env=` from one helper fails the scan test;
  dropping the hooksPath entry fails the hook test; dropping the
  `make_non_dumpable` call fails the runner test.
- The ruff and mypy ratchets report 0 regressed, and
  `gen_autonomy_matrix.py --check` passes (`core/auth.py` line numbers may
  shift).
- **After deploy, in the pod:**
  - start a build and confirm `cat /proc/<run.py pid>/environ` is denied
    from a same-uid shell;
  - confirm a `git commit` the server makes in a worktree with a planted
    failing hook succeeds;
  - confirm `gh` and `git push` from the PR endgame still work: a real
    task opens its PR.
