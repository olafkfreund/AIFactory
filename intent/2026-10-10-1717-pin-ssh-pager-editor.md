---
status: draft
issue: 1717
author: olafkfreund
---

# Intent: agent-written core.sshCommand, core.pager and core.editor reach server git

Follow-up to #1689, which pins `core.fsmonitor` and set these three keys aside
(#1689 spec, lines 25-40 and 163-165, on the #1689 branch). Still open on dev
at c59a0ea6: no commit pins any of the three.

## Problem

An agent can put values into a worktree's `.git/config`, either with
`git config ...` or by editing the file. `validate_git`
(`apps/backend/security/git_validators.py:50-87`) only rejects these keys when
they come as `git -c` options. Task worktrees share the project's
`.git/config`, so one planted value applies to every later server git call on
that project, for every task.

When the server runs git, `child_env` (`apps/backend/core/child_env.py:55-61`)
pins only `core.hooksPath`, and `core/worktree.py::_git_env` (`:90-100`) pins
nothing. So git uses whatever the agent planted.

- **core.sshCommand: reachable. The server runs the agent's command.**
  `maybe_push_workspace_branch` (`apps/backend/core/workspace_fetch.py:118-137`)
  reads the origin URL, which the agent controls. `authed_push_url`
  (`apps/backend/core/git_credentials.py:72-77`) passes a non-GitHub ssh URL
  through unchanged, and `git push` runs the planted `core.sshCommand`.
  We reproduced this against the real function with git 2.55.0: the planted
  script ran twice during the push. Other fetch, pull and push calls to origin
  are exposed the same way: `apps/web-server/server/routes/pr.py`
  (`:202`, `:306`, `:325`), `apps/web-server/server/routes/git.py`
  (`:1013`, `:1064`), `apps/backend/core/worktree.py:378`,
  `apps/backend/pfactory/tfactory_client.py` (`:401`, `:462`) and
  `apps/backend/runners/github/` (`context_gatherer.py`,
  `services/parallel_orchestrator_reviewer.py`).
- **core.pager: not reachable today.** Git resolves the planted pager, but
  every server git call captures stdout and none passes `-p`. Git starts no
  pager without a tty. A future call that has a tty or uses `-p` would run it.
- **core.editor: not reachable today.** Every server commit passes `-m` or
  `--no-edit`. `merge` and `pull` open an editor only when there is a tty.
  `rebase --continue`
  (`apps/web-server/server/services/pr_endgame.py:994-995`) already passes
  `-c core.editor=true`. A future commit path without `-m` would run it.

The agent runs with `permission_mode: "bypassPermissions"`
(`apps/backend/core/client.py:1054`). We found no hook that stops it writing to
`.git/config`. #1719 covers that.

## Proposed outcome

- No server git call runs an `ssh` command, pager or editor that an agent
  wrote into repo config.
- For each of the three keys there is either a pin that every server git
  path inherits, or a written "unreachable" argument backed by a test.
- A regression test plants all three keys and shows that none of them runs on
  the workspace push path.

## Affected users and systems

- Operators of AIFactory deployments (the control-plane pod and build Jobs).
  The planted command runs as the server process, with the server's
  filesystem and network access.
- Every task in a project whose `.git/config` an agent has changed.
- Projects with an ssh origin. SSH remotes are a supported project URL
  (`apps/web-server/server/routes/projects.py:147-150` accepts `ssh://` and
  `git@host:path`).
- Code: `apps/backend/core/child_env.py` (and the web-server wrapper
  `apps/web-server/server/utils/subprocess_env.py`),
  `apps/backend/core/worktree.py::_git_env`,
  `apps/backend/core/workspace_fetch.py`,
  `apps/backend/core/git_credentials.py`, `tests/test_child_process_env.py`.

## Constraints

- Every server git path has to get the fix, not only the one the issue names.
  `_git_env` only gets the pins after #1689 step 3 rebuilds it on `child_env`.
- This branch includes neither #1689 (commit a85edffd, the `fsmonitor` pin)
  nor #1688 (`github_env` on `fix/1688-github-token-per-call`, which also adds
  `GIT_CONFIG_*` entries). The fix must not drop or reorder entries that
  `child_env`, #1689 or #1688 put into `GIT_CONFIG_*`.
- Precedence:
  - A command-line `-c` beats an environment pin, so the `pr_endgame`
    `rebase --continue` call keeps working.
  - `GIT_SSH_COMMAND`, `GIT_EDITOR` and `GIT_PAGER` beat their config keys.
- Do not break operators who set `core.sshCommand` in global or system config,
  for example to choose a key file. Nobody has checked yet whether any image
  or deployment relies on this.
- Keep `tests/test_no_unscrubbed_spawn.py` and the #1962 autonomy-matrix gate
  green. The fix may need a row in `docs/static/compliance/autonomy-matrix.json`.

## Open questions

1. **core.sshCommand:** pin it in `child_env` (breaks operators who set it
   globally), or make the push and fetch paths refuse non-https origin URLs so
   the key is never reached? The second option drops ssh remotes, which
   projects accept today.
2. **Operator override:** keep `GIT_SSH_COMMAND` from the server environment as
   the supported way to override the pin, or strip it in `child_env` too?
3. **core.pager and core.editor:** pin them anyway as defence in depth
   (`cat` / `true`), or close them with an "unreachable" argument and a test?
4. **Order:** wait for #1689 to merge and rebase onto it, or fold this into the
   #1689 branch? Should #1719 (the guard on agent writes to `.git/`) land first
   and narrow this issue?
5. **Scope:** only the three keys in the issue, or also `protocol.ext.allow`
   with an `ext::` origin, `credential.helper`, `diff.*.command` and similar
   keys? The wider scope needs a pin list or an allow list rather than three
   fixed pins.
