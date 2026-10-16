---
status: approved
issue: 1745
intent: intent/2026-10-16-1745-git-config-pins.md
---

# Spec: pin the remaining agent-writable git config keys

## Design

Checked against `origin/dev` fb97ccd6 with git 2.55.0. The fixes are pins:
the same `GIT_CONFIG_COUNT` env entries `child_env()` already appends. Git reads
command-scope config after system, global and repo config. A pin therefore wins
over a single-valued key, and an empty value resets a multi-valued list.
Verified in a temp repo: with `core.askPass=repo-value` in the repo and the
env pin `core.askPass=` (empty), `git config --get core.askPass` prints an
empty value. With a repo `credential.helper` followed by the env reset, the
list ends in the reset. A `git -c` flag still beats the env pins, which is the
documented way to override them.

### D1. Pins added to `child_env()` (after the six existing ones)

| key | value | why this value |
|---|---|---|
| `core.askPass` | `` (empty) | Disables a repo askpass. The `GIT_ASKPASS` env var still takes precedence over `core.askPass`, so the workspace clone flow (`project_workspace_service.py:153-176`) keeps working. |
| `credential.helper` | `` (empty) | Resets every helper from system, global and repo config, both `credential.helper` and `credential.<url>.helper`. |
| `credential.https://github.com.helper` | `!gh auth git-credential` | Same helper `github_env` (`git_credentials.py:47,90-93`) adds. With a token, `github_env` appends its own reset and this helper again, so nothing changes. Without a token, an operator who ran `gh auth login` can still push. |
| `sequence.editor` | `true` | Matches the `core.editor` pin. `rebase -i` would otherwise run the repo value. |
| `remote.origin.uploadpack` | `git-upload-pack` | Git's default. Server fetches name `origin`. |
| `remote.origin.receivepack` | `git-receive-pack` | Git's default. Server pushes name `origin`. |

Plus `env.setdefault("GIT_PAGER", "cat")`. `GIT_PAGER` takes precedence over
`pager.<cmd>` and `core.pager` (git `pager.c::git_pager`). That covers
`pager.<cmd>`, which has no fixed key to pin. An operator's own `GIT_PAGER`
stays, the same rule the existing docstring gives for `GIT_SSH_COMMAND`.

The pins move from inline code to a module tuple and one function,
`pin_git_config(env, *, editor="true", pager="cat")`. `child_env()` calls it.
`editor` sets both `core.editor` and `sequence.editor`. `pager` sets
`core.pager` and the `GIT_PAGER` default.

### D2. Keys that cannot be pinned

- `uploadpack.packObjectsHook`: git reads it only from protected config
  (system, global, command line) and ignores it in repo config, so a planted
  value already has no effect. No pin is needed. An empty pin is not possible
  anyway, because git would try to run the empty value.
- `filter.<x>.clean/smudge/process`, `merge.<x>.driver`: the driver name is
  arbitrary, so there is no fixed key to pin. Server `merge-tree` and `add`
  exposure is tracked in #1761. A per-call `-c` cannot name an unknown driver.
  An attribute override (`GIT_ATTR_SOURCE`) would also turn off LFS filters.
  Not in this task.
- `diff.<x>.command/textconv`, `diff.external`: done by #1718, which adds
  `--no-ext-diff --no-textconv` on every server diff call and scans for it.
- `url.<x>.insteadOf` / `pushInsteadOf`: accepted, no change. A rewrite runs
  no program. `ext::` is pinned off (`protocol.ext.allow=never`). The token
  helper is scoped to `https://github.com`, so a rewrite to another host is
  never offered the token. What remains is a push or fetch going to a host the
  agent picked. That is a data-flow issue, not code execution, and is out of
  scope.
- `remote.<other>.uploadpack`: only `origin` is pinned. No server git call
  uses another remote name (`git grep '"push"'` and `"fetch"` on dev).

### D3. Web terminal

`PTYSession.start()` (`apps/web-server/server/pty/session.py:55-62`) applies
`pin_git_config` last, after `self.env`. The values used:

- `editor`: `VISUAL`, else `EDITOR`, else `vi`
- `pager`: `PAGER`, else `less`

These are git's own fallbacks, so `git commit` and `git log` behave as they did.
Hooks, fsmonitor, sshCommand, askPass and credential are pinned as on the
server.

It does not go through `child_env()`. That function drops `*_TOKEN`/`*_KEY`
names and denied keys, which would remove `CLAUDE_CODE_OAUTH_TOKEN`
(`pty/manager.py`), `GITHUB_TOKEN` and the user's own keys that the terminal
needs. `subprocess_env.py`'s docstring makes the terminal the deliberate
exception for env scrubbing. This change adds only the git pins.

### D4. Autonomy matrix

`scripts/gen_autonomy_matrix.py` gets a section `B2. Child-process git config
pins`. Its rows are produced by calling `child_env()` with `GIT_CONFIG_COUNT`
and `GIT_PAGER` unset, and it lists each `(key, value)` plus `GIT_PAGER`. The
section has one derived control id, `sandbox.git_config_pins`, with an entry in
`docs/compliance/control-objectives.toml`. `--check` gets a row floor equal to
the pin count. The generated md and json are regenerated.

## Alternatives rejected

- **Route the terminal through `child_env()`.** It strips the OAuth token and
  `*_TOKEN` names the terminal needs (D3).
- **Server editor and pager values in the terminal.** `core.editor=true` makes
  `git commit` abort with an empty message. Rejected for interactive use.
- **Credential reset with no github.com helper.** That breaks pushes on hosts
  that use `gh auth login` without `GITHUB_TOKEN`, whose global `.gitconfig`
  helper the reset would clear.
- **Config scan before each server git call.** There are about 86 call sites
  and no single choke point. A scan also has a check-then-use race. Pins need
  no call-site change.
- **`--no-ext-diff`-style flags per call for filters and merge drivers.** No
  such flags exist for them. That work is #1761.

## Risks

- An operator who relies on a non-gh host credential helper (`store`,
  libsecret, a GitLab helper) for factory git loses it, both on the server and
  in the web terminal. The fix for them is `GITHUB_TOKEN` or `gh auth login`.
  For a one-off command in the terminal: `git -c credential.helper=<x>`.
- The terminal no longer runs repo hooks, for example a pre-commit framework.
  That is the same trade-off #1680 made for the server. A user can run
  `git -c core.hooksPath=.git/hooks ...` for one command.
- Tool children without `GITHUB_TOKEN` can now reach the server user's
  `gh auth` credential for github.com, the same as a host where
  `gh auth setup-git` was run. This is small: the server user's gh login is
  already the factory's GitHub identity.
- Tests that assert `GIT_CONFIG_COUNT == "6"`, `"7"` or `"9"`
  (`tests/test_child_process_env.py:74,93,316`, `tests/test_github_env.py:121`)
  change with the pin count.

## Verification

- `tests/test_child_process_env.py`: a temp repo with
  `core.askPass`, `credential.helper`, `sequence.editor` and
  `remote.origin.uploadpack` set to marker values. Under `child_env()`,
  `git config --get` / `--get-all` shows the pinned values, and the last
  credential helper is the gh one. No planted program is run.
- A terminal test: `PTYSession` with ptyprocess spawn patched captures the env.
  It has the pins, `core.editor` is the user's `EDITOR`, and
  `CLAUDE_CODE_OAUTH_TOKEN` from `self.env` is still present.
- The regression test fails on dev (no pins) and passes after the change.
- `python scripts/gen_autonomy_matrix.py --check` and
  `tests/test_gen_autonomy_matrix.py` pass.
- `ruff check`, `ruff format --check` on the touched files.
