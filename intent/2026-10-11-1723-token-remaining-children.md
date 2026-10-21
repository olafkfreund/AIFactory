---
status: approved
issue: 1723
author: olafkfreund
---

# Intent: the GitHub token must not reach the four children #1688 left out

Follow-up to #1688 (PR #1726, bf4fd672). Its spec deferred these paths to this
issue (`spec/2026-10-10-1688-github-token-per-call.md:53`, `:193`, `:243`).
Not fixed on dev: the branch has no commits beyond `origin/dev` (e176db99), and
no commit or PR references #1723.

## Problem

Four paths still give a child process the GitHub token, either in its
environment or in a long-lived file. Since #1671 that token is the App
installation token, which can write to every repo the installation covers.

1. **Agent runner.** `RUNNER_KEEP` starts with `*GITHUB_KEEP`
   (`apps/web-server/server/utils/subprocess_env.py:47-48`), so
   `make_subprocess_env()` passes `GITHUB_TOKEN`/`GH_TOKEN` to every runner
   the web-server starts: build, PR review, spec creation, clarification,
   changelog and insights. Since #1692 (38ea8a79) the non-Claude agent CLIs
   get a scrubbed env, but the runner process and the Claude CLI the SDK
   starts from it still hold the token, so the agent's tools can read it.
   This is the least trusted child and the worst of the four.
2. **Worktree git calls.** `_git_env()` in `apps/backend/core/worktree.py:92-110`
   keeps the token for eight git calls and the `gh auth setup-git` call. `_setup_git_credentials()` (`:419-441`)
   runs `gh auth setup-git` before every #1106 base fetch, which rewrites the
   global git config on every build.
3. **Push URL.** `authed_push_url` (`apps/backend/core/git_credentials.py:119-169`)
   puts the raw token in `GIT_PASS` for the whole push
   (`workspace_fetch.py:139`, `pfactory/tfactory_client.py:397`).
4. **Web login.** On installs with no server token, `start_github_auth`
   (`apps/web-server/server/routes/github.py:633-646`) runs
   `gh auth login --web` with no `GH_CONFIG_DIR`, so gh stores the token in
   `~/.config/gh/hosts.yml` indefinitely.

The guard test `test_web_server_has_no_env_token_path`
(`tests/test_github_env.py:252-260`) misses all four. It scans only
`apps/web-server/server`, and its search strings do not match the
`*GITHUB_KEEP` spread. The "grep exemptions" in the issue are this scan scope;
the file has no exemption list.

## Proposed outcome

- No child that the web-server or backend starts has `GITHUB_TOKEN`, `GH_TOKEN`
  or `GIT_PASS` in its environment. Children that need GitHub get it through
  the existing per-call `github_env()`.
- No build writes global git or gh config.
- Pushes, the #1106 base fetch and the web login still work.
- The guard test covers `apps/backend` as well and fails on any of the four
  patterns, including a `GITHUB_KEEP` spread.

## Affected users and systems

- Operators of the web-server and control-plane pod, and owners of the repos
  it builds.
- Every runner the web-server starts (seven modules under
  `apps/web-server/server/services/` call `make_subprocess_env`).
- Every build that creates a worktree, plus workspace and TFactory pushes,
  most of which run in kubejob pods.
- Local and dev installs that log in with `gh auth login --web`.
- Files: `subprocess_env.py`, `core/child_env.py`, `core/worktree.py`,
  `core/git_credentials.py`, `routes/github.py`, `tests/test_github_env.py`.

## Constraints

- Reuse `github_env()` (`git_credentials.py:64-112`) and `sweep_github_dirs`.
  Do not add a second mechanism.
- Do not break `workspace_fetch` push, the `tfactory_client` force-push, the
  LFS push or the #1106 base fetch. A missing credential must still fail fast
  (`GIT_TERMINAL_PROMPT=0`), not hang.
- No global config writes in the control-plane pod or build Job.
- The installation token expires, so build the per-call dir when the call is
  made. Do not cache it for a whole build or agent run.
- The wider test must not flag expected matches: `_STRIPPED` and the askpass
  script text in `git_credentials.py`, and the `GIT_PASS_FILE` askpass in
  `project_workspace_service.py:141-187`.
- The runner reads the token in-process today and must keep working:
  `runners/github/runner.py:92` (PR review), `providers/factory.py:196`
  (GitHub Models), `core/mcp_credentials.py:189-197` (GitHub MCP), plus the
  worktree fetch and the pushes above.
- Overlaps #1717 (PR #1748, open, `core/child_env.py`). #1692 is merged and
  left `RUNNER_KEEP` as is.
- Out of scope: the `/proc/<child>/root` and ptrace exposure (#1688
  follow-up 2).

## Open questions

1. The runner's own code needs the token (see Constraints), but no `gh` use
   turned up under `apps/backend/prompts` or `apps/backend/agents`, so the
   agent's tools seem not to. Is it acceptable for the runner process to keep
   the token as long as the agent's Claude CLI and tools do not get it, or must
   the runner itself never hold it in env?
2. With no server token, keep `gh auth login --web` but point it at a managed
   `GH_CONFIG_DIR` (token still on disk), or remove web login and require
   `GITHUB_TOKEN` or the App?
3. `gh auth setup-git` in `worktree.py`: replace it with a per-call helper on
   the base fetch, or delete it once every fetch and push caller uses
   `github_env()`?
4. Fix all four paths here, or split the runner path into its own issue?
5. Wait for #1717 / PR #1748 to merge first, or start now and rebase?

## Approver decisions (2026-10-11)

- The runner process may hold the GitHub token; the agent's Claude CLI and tools must never receive it.
- Keep `gh auth login --web` but point it at a managed GH_CONFIG_DIR.
- Replace `gh auth setup-git` in worktree.py with a per-call credential helper.
- Fix all four paths in this issue.
- Start now and rebase (#1717 has merged).
