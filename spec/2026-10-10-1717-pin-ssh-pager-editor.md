---
status: approved
issue: 1717
intent: intent/2026-10-10-1717-pin-ssh-pager-editor.md
---

# Spec: pin core.sshCommand, core.pager and core.editor in child_env

## Design

Checked against `origin/dev` 1deb7faa and the branches
`origin/fix/1689-fsmonitor-git-config-audit` (PR #1722) and
`origin/fix/1688-github-token-per-call` (PR #1726). This branch (cf2fa70f)
holds only the approved intent.

### Proposed answers to the intent's open questions

These are proposed defaults for the approver to confirm or change.

1. **Q1 core.sshCommand: pin it.** Append `core.sshCommand=ssh` (git's own
   default) to the `GIT_CONFIG_*` pins in `child_env`. Do not refuse
   non-https origins: `apps/web-server/server/routes/projects.py:147-150`
   accepts ssh remotes today. Env config beats repo config, and every server
   git path builds on `child_env`. No default install sets `core.sshCommand`
   (a grep of `Dockerfile`, `charts/` and `docker-compose.yml` finds no ssh,
   `sshCommand` or openssh), so only operators with a custom global setting
   are affected, and Q2 gives them a way out.
2. **Q2 operator override: keep it.** `GIT_SSH_COMMAND`, `GIT_PAGER` and
   `GIT_EDITOR` set in the server environment pass through `child_env`
   unchanged (`is_denied_env_key` returns False for each and none matches
   `_CREDENTIAL_NAME`). Git ranks them above config, so they are the
   documented override. The agent writes `.git/config`, not the server's env,
   so stripping them would remove the only override and add no security. See
   Risk 1 for the one way project files can reach a child env.
3. **Q3 core.pager and core.editor: pin them** as `core.pager=cat` and
   `core.editor=true`. Four more lines cost less than an "unreachable"
   argument that goes stale when someone adds a commit without `-m` or a tty
   path. `cat` is git's "no pager" value, so no process starts. `true` matches
   the precedent at `pr_endgame.py:995`; a commit without `-m` then aborts on
   an empty message instead of running anything.
4. **Q4 order: do not fold into #1689**, which is already merging. After
   #1722 and #1726 merge, rebase this branch onto dev and append the new pins
   after #1689's three. Do not wait for #1719: it guards writes through the
   agent's tools, but the agent can still edit the file directly.
5. **Q5 scope: only the three keys now.** `protocol.ext.allow` is already
   pinned by #1689. A follow-up issue covers the repo-config keys a fixed pin
   cannot: `credential.helper`, `core.askPass`, `sequence.editor`,
   `filter.*.clean/smudge/process`, diff and merge drivers,
   `uploadpack`/`receivepack`, and `url.*.insteadOf` outside LFS. The likely
   shape is an allow list, or `GIT_CONFIG_NOSYSTEM` plus a validated
   `.git/config`. Today server `git diff` calls only use `--name-only`,
   `--name-status` or `--shortstat` (`worktree_merge.py:181/337/715`,
   `projects.py:1106`, `files.py:920`), #1688 pins `credential.helper` for
   GitHub, and #1690 refuses `url.*` rewrites on the LFS push.
6. **Regression test and compliance:** a real-git test plants all three keys
   and shows none runs on the workspace push path (intent outcome 3). The
   proposed "add or extend an autonomy-matrix row" cannot be done; see
   Alternatives rejected.

### Change: one code file, one test file, the CHANGELOG

**1. `apps/backend/core/child_env.py`**, after #1689's
`protocol.ext.allow` pin (#1689 branch `:59-66`, entries `n`, `n+1`, `n+2`):

- Append three pairs in the same style:
  `GIT_CONFIG_KEY_{n+3}=core.sshCommand` / `ssh`,
  `{n+4}=core.pager` / `cat`, `{n+5}=core.editor` / `true`.
- `GIT_CONFIG_COUNT` becomes `str(n + 6)`.
- Extend the docstring by one sentence: the pins also cover the ssh command,
  pager and editor, and an operator overrides them with `GIT_SSH_COMMAND`,
  `GIT_PAGER` or `GIT_EDITOR` in the server environment.

No other code changes. Every server git path inherits the pins:
`workspace_fetch.py:120` (`_git`), `git_credentials.py:72-77` (non-GitHub
branch yields `child_env()`), `worktree._git_env` (rebuilt on `child_env` by
#1689), `server/utils/subprocess_env.py:50`, and #1688's `github_env`, which
reads `GIT_CONFIG_COUNT` and appends after it. The `runners/github` git spawns
pass no `env=`, but the runner process itself starts with
`make_subprocess_env()` (`pr_review_service.py:255`), so its `os.environ`
already holds the pins.

**2. `tests/test_child_process_env.py`** (root `tests/`: outside the ratchet,
default ruff applies, the file's `# ruff: noqa` covers S603/S607):

- Update the hard-coded counts #1689 adds (`GIT_CONFIG_COUNT == "4"` at
  `:74` and `:222`, `"3"` at `:87`) to `"7"` and `"6"`. Assert keys `n+3`,
  `n+4`, `n+5` are `core.sshCommand`, `core.pager`, `core.editor` with values
  `ssh`, `cat`, `true`, appended after the existing `credential.helper` entry
  at index 0. Left alone, these counts fail the PR's own tests.
- New `test_real_git_ssh_pager_editor_pinned(monkeypatch, tmp_path)`, built
  like `test_real_git_hook_disabled_by_child_env` (`:166`):
  - `GIT_CONFIG_GLOBAL=/dev/null`, `GIT_CONFIG_NOSYSTEM=1`; delenv
    `GIT_SSH_COMMAND`, `GIT_SSH`, `GIT_PAGER`, `PAGER`, `GIT_EDITOR`,
    `VISUAL`, `EDITOR`, `GIT_CONFIG_COUNT`.
  - Set `WORKSPACE_URI` to a dummy value (`maybe_push_workspace_branch`
    returns early without it, `workspace_fetch.py:109`).
  - A repo with one commit at `tmp_path/.aifactory/worktrees/tasks/<spec>`,
    origin `ssh://git@127.0.0.1:1/r.git` (refused port, no DNS).
  - Plant all three keys as scripts that write a marker file.
  - **Positive control first:** a bare `git push` without pins creates the
    marker. Without this the test could pass because the plant does nothing.
  - Remove the marker, call `maybe_push_workspace_branch(project_dir, spec)`,
    assert no marker.
  - In the planted repo, without pins `git var GIT_PAGER` and
    `git var GIT_EDITOR` print the planted scripts (positive control); under
    `child_env()` they print `cat` and `true`. No tty needed: git resolves
    them as it would at run time.
  - Q2 in place: with `GIT_PAGER=less` and `GIT_EDITOR=vi` in `os.environ`,
    the same calls print `less` and `vi`, and a set `GIT_SSH_COMMAND` comes
    through `child_env()` unchanged.
  - `timeout=30` on every spawn. If CI has no `ssh` binary the marker still
    stays absent and the positive control still runs the planted script.

**3. `CHANGELOG.md`**: one entry under `## [Unreleased]` / `### Security`,
naming `GIT_SSH_COMMAND`/`GIT_PAGER`/`GIT_EDITOR` as the override. Expect a
conflict with the #1689 and #1688 entries; keep both sides.

## Alternatives rejected

- **Refuse non-https origins:** breaks ssh remotes that projects accept (Q1).
- **Strip `GIT_SSH_COMMAND`/`GIT_PAGER`/`GIT_EDITOR`:** removes the only
  operator override, adds no security (Q2).
- **A `GIT_PINS` tuple with a loop:** tidier and guards against index gaps,
  but rewrites #1689's lines. Six explicit pairs plus count asserts are
  enough; refactor if a fourth batch of pins appears.
- **Fold into #1689:** already merging (Q4).
- **Pin the wildcard keys** (`credential.helper`, `filter.*`, drivers,
  `url.*`, `core.askPass`): need a different mechanism; follow-up issue (Q5).
- **Empty `core.pager`/`core.editor`:** an empty editor falls back to
  `VISUAL`/`EDITOR`/`vi`, weaker than `true`.
- **`core.sshCommand=ssh -oBatchMode=yes`:** an operator-visible behaviour
  change, out of scope.
- **Pin in `_git_env` or at each call site:** call sites drift; `child_env`
  is the one place every path goes through.
- **Add an autonomy-matrix row:** `scripts/gen_autonomy_matrix.py:791-837`
  builds control ids only from tiers, overlay, wiring flags and gate entry
  points, and fails `--check` on any orphan id in
  `docs/compliance/control-objectives.toml`. #1689 added no row, and neither
  the matrix JSON nor the TOML cites `child_env`. A row would need a generator
  change, out of scope; raise it on the follow-up issue. This deviates from
  proposed answer 6 and needs the approver's sign-off.

## Risks

1. **Project `.aifactory/.env` can reintroduce an override.**
   `pr_review_service._load_env_file` (`:75-90`, used at `:274`) and
   `agent_service.py:1066-1080` copy any key not already present from
   `<project>/.aifactory/.env` into a child env. `GIT_SSH_COMMAND` and
   `GIT_CONFIG_PARAMETERS` (parsed after `GIT_CONFIG_COUNT`) both beat the
   pins, so whoever can write that file can bring a planted command back. Same
   hole as `LD_PRELOAD` through that file; a different trust boundary from
   `.git/config`. Not fixed here; put it on the follow-up issue with #1719.
2. **Operator config is silenced.** A global/system `core.sshCommand`, pager
   or editor, or one set through the operator's own `GIT_CONFIG_KEY_*`, loses
   to our pins. Legacy `GIT_SSH` ranks below `core.sshCommand`, so it is
   silenced too; nothing in the repo uses it. No default install sets any of
   these. Documented override: `GIT_SSH_COMMAND`/`GIT_PAGER`/`GIT_EDITOR`.
3. **The agent runner inherits the pins** (`make_subprocess_env`,
   `runner=True`, `subprocess_env.py:73`). Agent git gets `pager=cat`
   (harmless) and `editor=true`, so `git commit` without `-m` aborts with
   "empty message" instead of trying vi with no tty. The agent can still pass
   its own `-c` or env, affecting only its own children.
4. **A command-line `-c` beats the env pins.** Intended for
   `pr_endgame.py:994-995 -c core.editor=true rebase --continue`
   (unaffected). A future server call passing an agent-derived `-c` would
   bypass the pins; `validate_git` only covers the agent side.
5. **Rebase order.** Landing before #1722 would leave `_git_env` callers
   (`worktree.py:164,426,486,496,529,550,578,604,637`; `_run_git` at `:604`
   carries every `fetch`/`push` such as `:378`) unpinned and the count
   asserts wrong. Do not open the PR until #1722 and #1726 are on dev; run the
   full test file after the rebase.

## Verification

- `pytest tests/test_child_process_env.py tests/test_no_unscrubbed_spawn.py`
  green. No new spawns in code; the test's spawns sit under `tests/`.
- Mutation: remove each of the three pins in turn, confirm a test goes red,
  restore by re-editing (no stash/checkout).
- `ruff format --check` and `ruff check` on
  `apps/backend apps/web-server scripts tests`.
- Ratchet on `child_env.py`:
  `python scripts/cq_ratchet.py --staged --ruff "$(command -v ruff)" --config standards/ruff.toml --paths 'apps/backend/*.py' 'apps/web-server/*.py' 'scripts/*.py'`
  and the same with `--tool mypy --mypy "$(command -v mypy)" --config standards/mypy.ini`.
- `python scripts/gen_autonomy_matrix.py --check` stays green (needs the
  web-server deps, e.g. `yaml`). Nothing it cites moves.
- CodeQL: no new logging.
- Commit scope without `#`, e.g.
  `fix(security): child_env pins core.sshCommand, core.pager and core.editor (#1717)`.
