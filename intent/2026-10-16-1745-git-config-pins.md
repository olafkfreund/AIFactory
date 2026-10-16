---
status: approved
issue: 1745
author: olafkfreund
---

# Intent: pin the remaining agent-writable git config keys

Follow-up to #1689, #1717 and #1718. Checked against `origin/dev` fb97ccd6.

## Already done, not redone here

- **`.aifactory/.env` (risk 1 from #1717).** #1790 (a46b5e32) allow-lists the
  keys the loaders accept, and #1799 (d0c64187) moved project settings out of
  the agent-reachable clone into the DB. `GIT_SSH_COMMAND` and
  `GIT_CONFIG_PARAMETERS` can no longer come from that file.
- **Diff drivers and textconv.** #1718 (fd4296ed) makes every server
  `git diff`-family call pass `--no-ext-diff --no-textconv`, with a scan test.
- **Merge drivers and filters on server git.** Tracked by #1761
  (`git merge-tree`, `git add` with `filter.<x>.clean`). Not in this task.

## Problem

Task worktrees share the project's `.git/config`, so one value an agent plants
there applies to every later server git call on that project. `child_env()`
(`apps/backend/core/child_env.py:63-79`) appends `GIT_CONFIG_COUNT` pins for
hooksPath, fsmonitor, `protocol.ext.allow`, sshCommand, pager and editor.
Command-scope config is read after repo config, so these pins win. The repo can
still set these keys, which git runs as programs:

- `core.askPass`: run for a username or password prompt.
- `credential.helper` and `credential.<url>.helper`: run on every
  authenticated fetch or push. The server pushes through `github_env`
  (`apps/backend/core/git_credentials.py:52-97`). That appends a reset and a
  github.com-scoped `!gh auth git-credential` helper, but only when a token is
  set. Without a token, the repo's helper list runs as written.
- `sequence.editor`: run by `rebase -i`. It takes precedence over `core.editor`,
  so the existing editor pin does not cover it.
- `remote.<name>.uploadpack` / `.receivepack`: run for a local or ssh
  transport.
- `pager.<cmd>`: takes precedence over `core.pager` on a tty. No server spawn
  has a tty, but the web terminal does.

The web terminal (`apps/web-server/server/pty/session.py:56`) starts its shell
from `os.environ.copy()`, so git run in it gets none of the pins. Users open
that terminal in agent worktrees.

## Proposed outcome

- Every child that `child_env()` builds also pins `core.askPass`,
  `credential.helper` (reset, then the same github.com helper `github_env`
  uses), `sequence.editor` and `remote.origin.uploadpack`/`receivepack`.
  `GIT_PAGER` defaults to `cat`, which takes precedence over `pager.<cmd>`.
- The web terminal gets the same pins. Editor and pager keep the user's own
  values, so interactive git still works.
- The autonomy matrix gets a row listing the pins, generated from
  `child_env()`.

## Affected users and systems

- Server git (push, fetch, clone, merge) and every agent/tool child that
  `child_env()` / `make_subprocess_env()` builds.
- Users of the web terminal.
- Operators who rely on a host credential helper other than gh for git pushes
  from the factory.

## Constraints

- Keep the factory's credential flows working: the `github_env` token helper,
  `GIT_ASKPASS` for workspace clones (`project_workspace_service.py:153-176`),
  and `gh auth` without a token.
- The terminal keeps the env it has today (OAuth token, `*_TOKEN` names). Only
  the git pins are added.
- No probe strings in a shell. Tests use pytest with a temp repo.

## Open questions

1. Accept that an operator's non-gh host credential helper (`store`,
   libsecret) stops applying to factory git? (Proposed: yes, recorded as a risk.)
2. `url.<x>.insteadOf`: pin, scan or accept? (Proposed: accept. It runs no
   program, `ext::` is off, and the token helper only matches github.com.)
