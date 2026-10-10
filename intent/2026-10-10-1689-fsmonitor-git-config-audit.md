---
status: draft
issue: 1689
author: olafkfreund
---

# Intent: agent-written git config must not run commands in the server

Follow-up to #1680 / PR #1691 (`spec/2026-10-09-1680-child-process-env.md`,
Risks; `plan/2026-10-09-1680-child-process-env.md`, follow-ups).

## Problem

#1691 pins `core.hooksPath=/dev/null` for every child built by
`child_env()` (`apps/backend/core/child_env.py:51-57`), so a hook the agent
plants no longer runs in the server. Git still runs other commands named in
repo config, and the agent can still write that config:

- **Read side.** `core.fsmonitor` is not pinned. Server git calls that
  refresh the index (`status`, `add`, `commit`, `diff`, squash in
  `routes/git.py` and `routes/worktree_merge.py`) run whatever command it
  names. `core.sshCommand`, `core.pager`, `core.editor`, and filter and diff
  drivers are in the same class. Filter and diff drivers are named per driver,
  so no fixed key pin can turn them off.
- **Write side.** `validate_git` (`security/git_validators.py:51-89`) checks
  only `-c` and `--config-env`. `git config core.fsmonitor '<cmd>'`,
  `core.sshCommand` and `filter.x.smudge` all pass, even though
  `_is_dangerous_git_config` (`:31-48`) already lists those keys. Agent
  Write/Edit is allowed on the project tree (`core/client.py:741-763`) and
  has no PreToolUse hook (`:1030-1038` match only Bash and web tools), and shell
  redirects into `.git/config` are not checked.
- **Shared config.** A linked worktree's config is the main repo's
  `.git/config`. One task's write therefore reaches every task and every
  server git operation on that project.
- **Unpinned path.** `core/worktree.py:_git_env()` (`:90-100`) copies
  `os.environ`, dropping only the git location vars (`GIT_DIR` and kin). It
  has no secret scrub and no pins. On the kubejob path the server
  runs it in-process (`services/build_backend.py:1086-1089`). It is not in
  `tests/test_no_unscrubbed_spawn.py`.

Reproduced with real git: `git config core.fsmonitor "touch PWNED; false"`
run in a linked worktree, followed by `git status` in the main repo under
`child_env()`, created `PWNED`. Nothing shows in the UI, because git ignores
a failing fsmonitor.

## Proposed outcome

- A task agent that sets `core.fsmonitor`, or another fixed-name
  command key, in a worktree's config cannot make the web server, `run.py`
  or a gate run that command.
- An agent's `git config <dangerous-key>` is refused, or caught before the
  server runs git, with the same fail-closed behaviour as the #321 C4 `-c`
  check. The agent's ordinary `git config user.name/user.email` still works.
- Every server-side git spawn, including `core/worktree.py`, gets the same
  env and pins, and the spawn-scan test covers it.
- The residual risk that remains, such as filter and diff drivers if left
  open, is written down rather than implied to be covered.

## Affected users and systems

- Web server (control-plane pod): git routes, worktree merge, PR, squash.
- `run.py`, the agent and gates, through `make_subprocess_env(runner=True)`.
- Kubejob build path: `build_backend.py` in-process `WorktreeManager`, and the
  Job pod env allow-list (`:259-300`).
- Agent tool security: `security/git_validators.py`, `core/client.py` hooks.
- Tests: `tests/test_child_process_env.py` (it asserts the exact
  `GIT_CONFIG_KEY_n` index layout), `tests/test_no_unscrubbed_spawn.py`.

## Constraints

- Pins stay the last `GIT_CONFIG_KEY_n` entries, after any caller-supplied
  `GIT_CONFIG_*`, which are kept.
- Must not depend on bwrap. `AIFACTORY_BASH_SANDBOX=false` (k3d/Kind) has no
  filesystem boundary.
- Must not break Git LFS (`filter.lfs.*`, #1690). Do not pin `filter.*` or
  `lfs.*` here.
- Must allow the server's own `git config` calls (`core.bare`,
  `--global safe.directory`) and the agent's `user.*`.
- No replica-local state, such as an in-memory audit log. Multi-replica safe.
- Keep edits in separate hunks from #1674, #1688 and #1692, which touch the
  same strip lists and call sites. Pins must be safe for runners that later
  move onto `child_env` (#1692).
- Follow whatever block/verify pattern #1672 and #1673 settle on for
  agent-writable files the server trusts. Do not invent a third pattern.

## Open questions

1. **Which pins, and for whom.** Should we pin only `core.fsmonitor=false`,
   as the issue asks? Or also pin `core.sshCommand`/`GIT_SSH_COMMAND`,
   `core.pager`/`GIT_PAGER` and `core.editor`/`GIT_EDITOR`? Should the pins
   apply to every `child_env` caller, including the agent and builds as
   hooksPath does now, or only to server git spawns? An ssh pin changes how
   agents push to ssh remotes.
2. **Write side.** Pick one or a combination:
   - (a) block `git config <dangerous-key>` in `validate_git`
   - (b) add a PreToolUse guard on Write/Edit to `.git/` paths, resolving the
     gitdir and commondir
   - (c) have the server check `.git/config` before it runs git

   Should a hit fail the task, strip the key, or only log it?
3. **Filter and diff drivers.** Accept them as documented residual risk, or
   neutralise them per call (`--no-ext-diff`, `--no-textconv`), where LFS
   allows it?
4. **`core/worktree.py:_git_env()`.** Fold it into #1689, so it gets the
   scrub, the pins and spawn-scan coverage, or file it as its own issue?
5. **Kubejob Job pods.** Add the same pins to the Job env allow-list now, or
   leave Jobs unchanged, as #1691 did?
