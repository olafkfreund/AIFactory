---
status: draft
issue: 1723
intent: intent/2026-10-11-1723-token-remaining-children.md
---

# Spec: the GitHub token must not reach the four children #1688 left out

## Design

Checked against `fix/1723-token-remaining-children` at f4583d66 (rebased on
`origin/dev`, #1717 merged). One mechanism throughout: `github_env()`
(`apps/backend/core/git_credentials.py:64-112`) builds a 0700 per-call
`GH_CONFIG_DIR` from `github_token()` at call time, strips
`GITHUB_TOKEN`/`GH_TOKEN`/`GIT_PASS`, resets inherited credential helpers and
points git at `gh auth git-credential` for `https://github.com` only. No new
helper is added. The approver decisions in the intent set the four choices
below.

### 1. Agent runner: the runner keeps the token; the Claude CLI never gets it

Decision: the runner process may hold the token; the agent's Claude CLI and
its tools must not.

- `RUNNER_KEEP` (`apps/web-server/server/utils/subprocess_env.py:47-55`) is
  **unchanged**. The runner reads the token in-process for PR review
  (`runners/github/runner.py:92`), GitHub Models (`providers/factory.py:196`),
  the GitHub MCP (`core/mcp_credentials.py:189-197`), the worktree fetch and
  the pushes.
- The Claude CLI gets the token only where an SDK call site does not pass
  `get_agent_env_blanks()` (`apps/backend/core/auth.py:177-195`). That function
  already blanks `GITHUB_TOKEN` and `GH_TOKEN` (`_AGENT_ENV_DENY_EXACT`,
  `auth.py:149-150`), and `core/client.py:608-615` and
  `core/simple_client.py:87-89` already use it. Three sites build
  `ClaudeAgentOptions` directly with no `env=`, so their CLI inherits the full
  runner env, token included:
  - `apps/backend/runners/insights_runner.py:183` (tools Read/Glob/Grep, which can read `/proc/self/environ`)
  - `apps/backend/runners/github/services/followup_reviewer.py:698`
  - `apps/backend/runners/ai_analyzer/claude_client.py:124`

  Each gets `env=get_agent_env_blanks()`, the same blanking the other
  sites use. The SDK merges `options.env` over the inherited env, so an empty
  value overrides the token. Agent tools (Bash and the rest) inherit the CLI's
  env, so this covers them too. The non-Claude CLIs are already scrubbed
  (#1692).

### 2. Worktree git calls: no token in env, no `gh auth setup-git`

Decision: replace `gh auth setup-git` with a per-call credential helper.

- `_git_env()` (`apps/backend/core/worktree.py:92-110`) drops
  `keep=GITHUB_KEEP`. With `runner=False`, `child_env` then strips the token
  through `_CREDENTIAL_NAME`. Eight of the nine calls are local (rev-parse,
  config, check-ignore, merge, stash) and need no credential. The `GITHUB_KEEP`
  import goes.
- `_setup_git_credentials()` (`:419-441`) and its call (`:385`) are deleted.
  Nothing writes global git or gh config during a build.
- The one network call, the #1106 base fetch (`:386-388`), runs as
  `with github_env(_git_env()) as env: self._run_git([...], timeout=..., env=env)`.
  `_run_git` (`:593-616`) gets an optional `env` parameter that defaults to
  `_git_env()`. `GIT_TERMINAL_PROMPT=0` comes from `_git_env()` and survives
  `github_env`, so a missing credential still fails fast. With no token,
  `github_env` yields the base env unchanged and the fetch uses whatever helper
  the host has, as before #1106 added `setup-git`.
- The image keeps its build-time global helper (`Dockerfile:438`).
  `github_env` resets inherited helpers for its own calls, so that helper does
  not matter on this path.

### 3. Push URL: `authed_push_url` built on `github_env`

`authed_push_url` (`git_credentials.py:119-169`) keeps its signature and its
contract: `(url, env)` are yielded together, and only an https github.com
remote is credentialed. Its body changes:

- Token present and `url.startswith(GITHUB_HTTPS_PREFIX)`: yield `url`
  **unchanged** with the env from
  `github_env({**child_env(), "GIT_TERMINAL_PROMPT": "0"})`. The URL needs no
  username: git asks the github.com-scoped helper.
- Otherwise: yield `url, child_env()` as today.
- `_ASKPASS_SCRIPT`, the temp askpass file, `GIT_USER` and `GIT_PASS` are
  deleted. `GIT_PASS` stays in `_STRIPPED` so a stray inherited value is still
  removed.

Callers do not change: `core/workspace_fetch.py:139` (push),
`pfactory/tfactory_client.py:397` (force-push plus `_remote_tip` ls-remote
with the same env). The LFS push paths (`routes/pr.py:334`,
`services/pr_endgame.py:90`) already run under `github_env` and are untouched.

### 4. Web login: managed `GH_CONFIG_DIR`, published like the App token

Decision: keep `gh auth login --web`, pointed at a managed `GH_CONFIG_DIR`.

- The managed dir is `get_data_dir() / "gh"` (`apps/web-server/server/paths.py:30`,
  that is `~/.aifactory/gh`), created 0700.
- `start_github_auth` (`apps/web-server/server/routes/github.py:633-646`)
  spawns with `env=child_env(extra={"GH_CONFIG_DIR": <managed dir>})`. gh
  then writes its token there, not to `~/.config/gh`.
- Consumers read the token through `github_token()`, which reads only the env,
  so the login token is published into the server env the same way the App
  token is. In `apps/web-server/server/services/github_app.py`, a new
  `load_web_login()` runs `gh auth token --hostname github.com` with
  `GH_CONFIG_DIR=<managed dir>` (this covers both file and keyring storage).
  On non-empty output it calls the existing `_publish()`. It runs:
  - in `main.py` lifespan, right after `github_app.start()` (`:110`), only when
    `not github_token()` and the App is not configured;
  - from the login wait task in `routes/github.py` on `returncode == 0`.

  From then on every child takes the per-call path: `run_gh_command`
  (`services/gh.py:33`), the worktree fetch and the pushes. No route needs
  its own `GH_CONFIG_DIR`.
- `_hosts_yml_has_token()` (`github_app.py:42-56`) also checks the managed
  dir, so the App still refuses to start next to a stored login token.
- No automatic migration of an existing `~/.config/gh/hosts.yml`. See Open
  question 1.

### 5. Guard test covers `apps/backend`

`test_web_server_has_no_env_token_path` (`tests/test_github_env.py:252-260`)
scans `apps/web-server/server` **and** `apps/backend`, excluding test
directories. Needles: `keep=GITHUB_KEEP`, `*GITHUB_KEEP`, `"setup-git"`,
`GIT_PASS"` and `"GIT_PASS":`. The assertion lists the hits it allows by
file, with no blanket exemption:

- `subprocess_env.py`: the `*GITHUB_KEEP` in `RUNNER_KEEP`. This is the
  approver's decision in Part 1, and the test names it with a comment.
- `git_credentials.py`: the `_STRIPPED` line.

`GIT_PASS_FILE` in `project_workspace_service.py:141-187` does not match
`GIT_PASS"`. The askpass script text goes away in Part 3.

A second test asserts that every `ClaudeAgentOptions(` call in `apps/backend`
(excluding tests) has an `env=` argument. This stops a fourth unblanked SDK
site from appearing (Part 1).

## Alternatives rejected

- **Drop `GITHUB_KEEP` from `RUNNER_KEEP`** and hand the runner a per-call dir.
  The approver ruled that the runner may hold the token. The runner also needs
  the raw token in-process (GitHub Models, MCP), which `GH_CONFIG_DIR` cannot
  give it.
- **Strip the token inside the runner at startup and keep it in a Python
  variable.** Every in-process reader (`github_token()`, `mcp_credentials`,
  `providers/factory.py`) would need a second source. That is a second
  mechanism, which the intent forbids.
- **Keep `gh auth setup-git` but point `GH_CONFIG_DIR` at a temp dir.**
  `setup-git` writes `~/.gitconfig` regardless, and the approver chose a
  per-call helper.
- **Keep the askpass design and pass the token in a 0600 file
  (`GIT_PASS_FILE`, as `project_workspace_service.py` does).** It works, but
  it is a second mechanism next to `github_env`.
- **Have `github_token()` read the managed `hosts.yml` itself.** That spawns
  `gh` or parses YAML on every child spawn, and it would add a backend
  dependency on a web-server path. Publishing once to env reuses the App's
  `_publish()` path.
- **Remove web login.** Rejected by the approver.
- **Split the runner path into its own issue.** Rejected by the approver.

## Risks

- **`gh` must be on PATH wherever a push or base fetch runs with a token.**
  The control-plane image has it (`Dockerfile:184`), and #1688 already relies
  on it for the web-server children. If a kubejob or local host has a token but
  no `gh`, the helper fails and the push or fetch fails fast instead of
  succeeding through askpass. Verify the build Job image before merge.
- **The runner still holds the token, and the agent runs as the same uid.**
  An agent's Bash can read `/proc/<runner pid>/environ` through its process
  tree. The approver accepted this, and it is the same class as the
  `/proc`/ptrace exposure that is out of scope (#1688 follow-up 2). This change
  removes the direct inheritance only.
- **Existing web-login installs.** Their token stays in `~/.config/gh`. With
  no migration, `github_token()` is empty, `github_env` yields the base env,
  and gh and git keep using the default dir as today. Nothing breaks, but those
  installs keep the long-lived file until the operator logs in again
  (Open question 1).
- **Local dev without a token and with an https remote.** Removing
  `setup-git` means the base fetch uses the host's own credential setup. A
  developer who relied on AIFactory running `setup-git` for them must run it
  once. On a headless host, `GIT_TERMINAL_PROMPT=0` makes this fail fast
  rather than hang.
- **Push semantics.** The URL no longer embeds `x-access-token@`. A remote
  that already carries a username still matches the
  `credential.https://github.com.helper` scope, because git matches the host.
- **SDK env merge.** If the SDK ever replaced the inherited env with
  `options.env` instead of merging, the three Part 1 sites would lose PATH.
  `client.py` already depends on the merge behaviour, so the risk does not
  change.

## Verification

- Unit tests, updated or added:
  - `tests/test_git_argv_credential_backend.py`, `tests/test_child_process_env.py:282-297`,
    `tests/test_workspace_fetch.py:169`: the vacuity guards change from
    `env["GIT_PASS"] == TOKEN` to `GH_CONFIG_DIR` set, the `hosts.yml` holding
    the token, no `GITHUB_TOKEN`/`GH_TOKEN`/`GIT_PASS` in env, the URL
    unchanged, and the dir removed on exit. A non-github host still gets plain
    `child_env()`.
  - New `worktree` test: `_git_env()` has no token. The base fetch runs under
    a `GH_CONFIG_DIR` with the github.com helper. `gh auth setup-git` is never
    spawned (patch `subprocess.run`, same pattern as `test_pr_endgame.py:95`).
  - New test: each of the three SDK sites builds options with
    `env["GITHUB_TOKEN"] == ""` when the token is set.
  - `github_app`: `load_web_login()` publishes the managed token, is skipped
    when a token or the App is present, and `_hosts_yml_has_token()` sees the
    managed dir. `start_github_auth` spawns with `GH_CONFIG_DIR` set to the
    managed dir.
  - Guard tests from Design Part 5 pass on the branch. They fail when the
    `*GITHUB_KEEP` is reintroduced in `worktree.py`, when `setup-git` returns,
    or when an `env=` is removed from an SDK site (mutation check, done by hand
    once).
- Commands: `pytest tests/test_github_env.py tests/test_git_credentials.py
  tests/test_git_argv_credential_backend.py tests/test_child_process_env.py
  tests/test_workspace_fetch.py apps/web-server/tests/test_pr_endgame.py
  apps/web-server/tests/test_workspace_argv_never_logged.py` plus the
  worktree tests, then the full suite and `ruff`.
- Runtime check on the dev control plane, one build end to end:
  - The base fetch succeeds.
  - `~/.gitconfig` mtime is unchanged.
  - `workspace_fetch` push and TFactory force-push land.
  - `tr '\0' '\n' </proc/<claude cli pid>/environ | grep -E '^(GITHUB_TOKEN|GH_TOKEN)='`
    shows empty values for an insights run.
  - No `aif-gh-*` dir is left after the build.
- Web login on a dev install with no token: the device flow completes,
  `~/.aifactory/gh/hosts.yml` (or the keyring) holds the token,
  `~/.config/gh/hosts.yml` is not created, and `/api/github/auth/check` reports
  authenticated.

## Open questions

1. Existing web-login installs keep the token in `~/.config/gh/hosts.yml`.
   Leave it and log a startup warning (proposed), move it into the managed dir
   automatically, or refuse to start until the operator removes it?
