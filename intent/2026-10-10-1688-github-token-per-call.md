---
status: draft
issue: 1688
author: olafkfreund
---

# Intent: the GitHub token must not sit in the environment of `gh` and `git` children

Follow-up to #1680 (merged as PR #1691). Its spec accepted this risk and named
this issue as the fix (`spec/2026-10-09-1680-child-process-env.md:136-139`).

## Problem

Since #1680, each child the web server starts goes through `child_env()`
(`apps/backend/core/child_env.py`). That strips the server's secrets, but
`keep=GITHUB_KEEP` puts `GITHUB_TOKEN` and `GH_TOKEN` back so `gh` and network
`git` can authenticate. 18 call sites in `apps/web-server/server` do this.

The server itself is non-dumpable (`main.py:753-776`). A child is dumpable
again after `execve`. So while a `gh` call, a `git push` or a `gh auth login
--web` runs, any process with the same uid in the pod, including an agent, can
read the token from `/proc/<pid>/environ`. This was reproduced on this
checkout. Nothing fails and nothing is logged. With the token an agent can push,
merge or approve outside the factory's gates.

The #1362 askpass helpers (`core/git_credentials.py`,
`project_workspace_service.py`) do not help here. They keep the token out of
argv, but they still pass it in `GIT_PASS`. `gh auth setup-git` makes it worse:
it installs `gh auth git-credential` as git's global credential helper, and that
helper reads the token from the environment. So every plain `git push` or `fetch`
depends on the token being in the environment.

## Proposed outcome

- No `gh` or `git` child of the web server has the GitHub token in its
  environment under any name. Reading `/proc/<pid>/environ` of such a child
  shows no token.
- `gh` calls, `git fetch` and `git push` to `https://github.com/` remotes keep
  working, and the PR, completion and endgame flows behave as before.
- The spec says plainly which exposure window is closed and which, if any,
  stays open.

## Affected users and systems

- Operators whose GitHub PAT or app token the server holds.
- The web server's 18 `child_env(keep=GITHUB_KEEP)` call sites (`routes/pr.py`,
  `routes/git.py`, `routes/github.py`, `services/gh.py`, `task_branch.py`,
  `pr_endgame.py`, `build_backend.py`, `copilot_dispatch_service.py`,
  `completion_orchestration.py`, `pr_data_service.py`,
  `project_workspace_service.py`).
- The two askpass helpers, and the four `gh auth setup-git` sites
  (`pr.py`, `pr_endgame.py`, `completion_orchestration.py`, `core/worktree.py`).
- Local and co-mounted builds. Kubejob pods have their own `/proc`, so they are
  not exposed through this path.

## Constraints

- The token must not appear in argv either: no URL credentials and no `-c`
  values. The #1362 argv tests keep passing.
- Keep the `child_env()` guarantees: the secret scrub, the `*_TOKEN`/`*_KEY`
  strip, and the `core.hooksPath=/dev/null` pin. Any extra git config is
  appended through `GIT_CONFIG_COUNT` without overwriting existing entries, and
  must work together with #1689.
- The server stays non-dumpable. Its own environment keeps the token, which
  arrives from a Secret and is read by the MCP probes.
- The token is resolved on every call, because settings and project routes
  change `os.environ` at runtime.
- The token is only offered to `https://github.com/` remotes.
- The root filesystem is read-only, so any helper file goes in `/tmp`. Cleanup
  must also run when the child crashes or times out.
- Whatever is chosen must also work for `git-lfs` (#1690). It must not block a
  GitHub App with installation tokens (#1671).
- No state shared across replicas (`replicaCount` is 1 today).

## Open questions

1. **Threat bar.** Is narrowing the window enough? For example, the token in a
   0600 `/tmp` file or a pipe is still readable by the same uid through
   `/proc/<pid>/fd` while the call runs. For `gh`, which has no askpass, that
   means a per-call `GH_CONFIG_DIR/hosts.yml`. Or must the gap be closed fully?
   That needs PID isolation (`sandbox.agent.pidNamespace`) or a separate uid
   for agents, which is a different change from credential plumbing.
2. **Scope.** Web-server children only, as the issue says? Or also the runner
   environment (`RUNNER_KEEP`) and the backend sites (`core/worktree.py`,
   `workspace_fetch.py`, `tfactory_client.py`)? Without them the token stays
   readable elsewhere in the same pod.
3. **`gh auth setup-git`.** Remove it everywhere and send all network git
   through the new mechanism? Or keep it, which keeps the token in the
   environment of any git that relies on it?
4. **Order against #1692.** Until #1692 lands, Codex and Gemini agents get the
   token directly through the runner environment, a worse path than this
   `/proc` race. Should #1692 go first?
5. **#1671.** Should this fix wait for, or include, a GitHub App with
   short-lived installation tokens, so that any token that leaks expires
   quickly?
