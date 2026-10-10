---
status: approved
issue: 1689
intent: intent/2026-10-10-1689-fsmonitor-git-config-audit.md
---

# Spec: agent-written git config must not run commands in the server

## Design

Four source files and three test files change. The fix has three parts:

- a read-side pin for `core.fsmonitor` in `child_env()`
- `core/worktree.py:_git_env()` rebuilt on `child_env()`
- a write-side check of `git config` in `validate_git`, plus a fix to the
  hook loop so every git segment of a command is validated

### Proposed answers to the intent's open questions

These are proposed defaults. The user confirms or changes them at this gate.
None of them is approved yet.

**Q1. Pins: only `core.fsmonitor=false`, for every `child_env` caller.**
It goes in next to `core.hooksPath`, so the agent, builds and server git all
get it, as they get hooksPath now. `core.sshCommand`, `core.pager` and
`core.editor` are not pinned. They are listed as residual risk, with a
follow-up issue.

- fsmonitor fires by itself on every index refresh. Real git 2.55 in the
  scratchpad: `GIT_CONFIG_KEY_0=core.fsmonitor`, `GIT_CONFIG_VALUE_0=false`
  stopped `git status` from creating `PWNED`. Without the pin it was created.
  The pin also overrode a value set through `[include] path=` and the
  linked-worktree repro from the intent.
- SSH remotes are supported (`apps/web-server/server/routes/projects.py:147-150`,
  `apps/web-server/server/services/project_workspace_service.py:127`). A
  `core.sshCommand` pin would break operators who set it globally, for
  example to pick a key file.
- The pager runs only on a tty, and server git captures stdout.
- `core.editor` is set per call where it matters. `-c` beats the env pins,
  so `apps/web-server/server/services/pr_endgame.py:967` is unaffected.

**Q2. Write side: (a) only. A hit refuses the command.**
`validate_git` refuses `git config` when it would set a dangerous key. The
command is refused, with the same fail-closed message pattern as the `-c`
branch (#321 C4). It is not stripped and not just logged. (b) and (c) wait
for the pattern #1672 and #1673 settle on (intent:83-84).

**Q3. Filter and diff drivers: residual risk.**
They are recorded under Risks. A follow-up issue covers `--no-ext-diff` and
`--no-textconv` on server `git diff` and `git log -p` calls. `filter.*` is not
added to any list, because LFS needs it (#1690).

**Q4. Fold `_git_env()` in.**
It becomes `child_env(keep=GITHUB_KEEP)` minus `_AMBIENT_GIT_VARS`, with
`GIT_TERMINAL_PROMPT=0` added. `core/worktree.py` is added to the spawn
scan.

**Q5. Job allow-list unchanged.**
`build_backend.py:259-300` is not touched. Inside the Job pod, `run.py`
builds its children with `child_env()` and `_git_env()`. That code applies the
pins, so forwarding `GIT_CONFIG_*` would only duplicate them.

**Beyond the Q2 default (needs explicit confirmation).**
Two additions go past the literal Q2 answer. Without them the write-side block
can be trivially bypassed, or leaves a confirmed command key open:

- **E1. Validate every git segment.** Today
  `security/hooks.py:188` and `:246` validate only the first segment that
  contains `git`. Checked: `validate_command('git status && git -c
  core.fsmonitor=x status')` returns `(True, '')`. This is a live hole in the
  existing #321 C4 check, and the new `git config` check would inherit it.
- **E2. Widen `_is_dangerous_git_config`.** Today it returns False for
  `gpg.program`, `credential.helper`, `core.askPass`, `core.gitProxy`,
  `diff.external`, `merge.<x>.driver`, `include.path` and
  `remote.<x>.uploadpack`. With `commit.gpgsign=true`, a commit made under
  both pins still ran an agent-set `gpg.program` (verified in the
  scratchpad). Server commit and squash would therefore run it. Intent
  line 28 says `filter.x.smudge` is already listed; it is not, and stays
  unlisted for LFS.

If either addition is declined, it moves to Risks as residual and gets a
follow-up issue.

### Changes

**1. `apps/backend/core/child_env.py:55-57` (Q1).**
Write hooksPath at index `n` and `core.fsmonitor=false` at `n + 1`, then set
`GIT_CONFIG_COUNT = n + 2`. Caller `GIT_CONFIG_*` entries keep the indices
below `n`, so the pins stay last (intent:71). Update the docstring to say it
disables hooks and fsmonitor. Keep this hunk inside lines 51-57, away from
the strip-list edits in #1674 and #1688.

**2. `apps/backend/core/worktree.py:90-100` (Q4).**
`_git_env()` returns `child_env()` with `GITHUB_KEEP` kept and
`GIT_TERMINAL_PROMPT=0` added, with every `_AMBIENT_GIT_VARS` key removed.
It no longer copies `os.environ` itself.

- **One fix point.** All 9 spawns in the file already pass `env=_git_env()`
  (lines 164-637).
- **Credentials.** `GITHUB_KEEP` is required, because the base fetch uses the
  gh credential helper (`worktree.py:376-378`, `:410-413`). This is the same
  call as the push path at `build_backend.py:953`.
- **No import cycle.** `core.child_env` imports only `core.auth`, which
  imports only stdlib and `factory_common`.
- **`trusted_plan.py:568-592` is unchanged.** It intersects with `_git_env()`
  and keeps `GIT_CONFIG_*`, so the result is the same.

**3. `apps/backend/security/git_validators.py:51-89` (Q2(a)).**
After the existing token loop, find the git subcommand. Skip global options;
`-C`, `-c`, `--config-env`, `--git-dir`, `--work-tree` and `--namespace` also
consume the next token. When the subcommand is `config`:

- **Check every token.** Run `_is_dangerous_git_config` on every
  non-option token after it, not only the first. This deviates from the
  literal Q2 answer: with the first-token rule,
  `git config -f .git/config core.fsmonitor x` passes, because its first
  non-option token is the path. Checking all tokens needs no option-arity
  table. A value that happens to match, such as `user.name "Ops Command"`, is
  refused. That fails closed and is rare.
- **Refuse edit and section modes.** Refuse `-e`, `--edit`, `edit`,
  `--rename-section`, `rename-section`, `--copy-section`. `GIT_EDITOR=<script>
  git config -e` writes any key, and a rename can move a harmless section
  onto `core`. Agents never need either.
- **Unparseable branch (`:58-66`).** Also refuse when the lowered string
  contains both `config` and a dangerous key prefix (`fsmonitor`,
  `command`, …). The simplest form is a substring check on `" config "` plus
  `_is_dangerous_git_config` over the whitespace-split words.

`git config user.name|user.email`, `--local core.bare false` and
`--global --add safe.directory <p>` still pass. The server's own `git config`
calls are subprocesses and never reach `validate_git`.

**4. `apps/backend/security/hooks.py:188`, `:246` (E1).**
Validate every segment whose `extract_commands` contains `cmd`. If none does,
fall back to `[command]`, as today. Return on the first refusal. This applies
to every validator (`git`, `rm`, `chmod`, …). Add a helper next to
`get_command_for_validation` in `security/parser.py` that returns all matching
segments, and use it in both loops.

**5. `apps/backend/security/git_validators.py:31-48` (E2).**
Add `gpg.program` and `gpg.<fmt>.program` (match on the `.program` suffix),
`credential.` prefix, `core.askpass`, `core.gitproxy`, `diff.external`,
the `.driver` suffix (`merge.<x>.driver`), `.uploadpack` and `.receivepack`
suffixes, `include.path`, and `includeif.` prefix. This widens the `-c`
block too. No agent prompt or validator test uses `-c` with any of these
keys (grep of `apps/backend/prompts` and `apps/backend/security`). The
server's own `credential.helper` uses (`worktree.py`, `routes/pr.py`) are
subprocess calls and do not reach the validator.

**6. `tests/test_no_unscrubbed_spawn.py:59-66` (Q4).**
Add `_BACKEND / "core" / "worktree.py"` to `_BACKEND_FILES`. The rebuilt
`_git_env` no longer copies `os.environ`, so the second scan stays green.

**Not changed:**

- `build_backend.py` (Q5)
- `filter.*` and `lfs.*` (Q3)
- per-call diff flags (Q3)
- Write/Edit hooks (Q2 b/c)

## Alternatives rejected

- **Pin `core.sshCommand`, `core.pager` and `core.editor` now.** It breaks
  operators' ssh key selection, and the pager and editor risks are small (see
  Q1). These go to a follow-up issue.
- **"First non-option token is the key" (literal Q2).** `git config -f <path>
  <key>` bypasses it. A full option-arity table for `git config` is brittle
  against new git options.
- **Write/Edit guard on `.git/` (b), or a server check of `.git/config` (c).**
  Either would invent a pattern before #1672 and #1673 settle theirs
  (intent:83-84).
- **Per-call `--no-ext-diff`/`--no-textconv`.** It touches every diff site
  in `routes/git.py` and `routes/worktree_merge.py`. This goes to a
  follow-up issue.
- **Pin or deny `filter.*`.** It breaks LFS (#1690).
- **Forward `GIT_CONFIG_*` to Job pods.** It duplicates what `run.py`
  already applies.
- **Keep `_git_env` separate and add only the pins.** It leaves the secret
  scrub missing and creates a second copy of the pin logic.
- **Append the pins to an inherited `GIT_CONFIG_PARAMETERS`.** It does
  override `GIT_CONFIG_COUNT` pins; that was checked. But the server's
  environment is set by the operator, not the agent, and an agent that sets it
  only affects its own git. This adds code for a case nobody can exploit.
- **Re-split `bash -c "…"` strings inside `validate_git`.** It patches one
  validator for a parser-wide gap (see Risks). It belongs in the follow-up
  that fixes `extract_commands` for every validator.

## Risks

1. **Direct writes stay open.** Write/Edit, a shell redirect into
   `.git/config` or `config.worktree`, or any script the agent runs can still
   set any key. The pins make fsmonitor and hooksPath harmless. For
   `core.sshCommand`, `gpg.program`, `credential.helper`, `core.askPass`,
   `core.gitProxy`, `include.path`, pager, editor and filter/diff drivers, the
   risk stays until (b) or (c) lands. The write-side check stops the plain
   `git config` path only.
2. **`bash -c` wrappers bypass every git check.** Checked:
   `validate_command('bash -c "git -c core.sshCommand=x fetch"')` returns
   `(True, '')`. This already applies to the existing `-c` check and to every
   validator. It needs a follow-up issue against `security/parser.py`.
3. **Filter and diff drivers stay open (Q3).** `diff.<x>.command` is
   blocked through `git config`. `diff.<x>.textconv` and `filter.<x>.*` are
   not blocked anywhere.
4. **WorktreeManager now runs with `core.hooksPath=/dev/null`.** Project
   pre-commit and merge hooks no longer run for commits and merges made
   through `_git_env` (`core/workspace/merge.py:229`, finalization, CLI local
   merges). The intent wants this, and the PR must say so.
5. **`_git_env` drops credentials.** It now strips `*_KEY`/`*_TOKEN`, except
   `GH_TOKEN`/`GITHUB_TOKEN`. A host credential helper that reads another
   token from env (GHE, GitLab, CodeCommit) fails the worktree base fetch.
   The failure is loud (`WorktreeError` at `worktree.py:391`), and the fetch
   path only wires `gh` (`:416-431`).
6. **E1 may newly refuse agent commands.** Segments that were never
   validated are now checked, for `rm` and `chmod` too. That is intended,
   but watch for agent retry loops after merge.
7. **Config values can be refused.** A value that matches the deny-list,
   such as `user.name "Ops Command"`, is refused. So are reads and removals
   of a dangerous key (`git config --get core.pager`,
   `git config --unset core.fsmonitor`), since the check does not look at the
   mode. This fails closed.
8. **Index layout changes.** Tests that assert exact `GIT_CONFIG_KEY_n`
   indices change. #1674, #1688 and #1692 must rebase onto
   `COUNT = n + 2`.
9. **Older git.** Before git 2.36, `core.fsmonitor=false` is treated as a
   hook path and runs `/bin/false`. That is harmless, and git ignores the
   failure.

## Verification

- **`tests/test_child_process_env.py`**
  - Update the layout tests (`:66-83`): hooksPath at `n`, fsmonitor at
    `n + 1`, `COUNT = n + 2`.
  - Add a real-git test. Create `tmp_path` with a linked worktree and run
    `git -C wt config core.fsmonitor "touch M; false"`. Then run `git status`
    in the main repo with `env=child_env()`: `M` is absent. A control run
    with `os.environ` creates `M`.
- **`_git_env` test**
  - `GIT_DIR` and `DATABASE_URL` are absent.
  - `GH_TOKEN` is kept.
  - `GIT_TERMINAL_PROMPT=0`.
  - Both pins are present.
- **`tests/test_command_sandbox.py`, through `validate_command`**
  - Refused:
    - `git config core.fsmonitor x`
    - `git config set core.sshCommand x`
    - `git -C d config core.pager x`
    - `git config -f .git/config core.fsmonitor x`
    - `git config --file=.git/config core.editor x`
    - `git config --global alias.x '!sh'`
    - `git config -e`
    - `git config --rename-section foo core`
    - `git status && git config core.fsmonitor x` (E1)
    - `git status && git -c core.fsmonitor=x status` (E1)
    - `git config gpg.program x` (E2)
    - `git -c credential.helper=x fetch` (E2)
    - `git config include.path x` (E2)
  - Allowed:
    - `git config user.name "A B"`
    - `git config user.email a@b.c`
    - `git config --get user.name`
    - `git config --local core.bare false`
    - `git config --global --add safe.directory /w`
    - `git commit -m config`
- **Command:** `pytest tests/test_child_process_env.py
  tests/test_no_unscrubbed_spawn.py tests/test_command_sandbox.py
  tests/test_worktree.py` and the trusted_plan tests are green.
- **Manual repro from the intent.** Run it under `child_env()` and under
  `_git_env()`: no `PWNED`.
- **Kubejob smoke (k3d, `AIFACTORY_BASH_SANDBOX=false`).** One build fetches
  the base branch through `_git_env` with the gh helper and succeeds, and
  `PWNED` is absent afterwards.
- **Job allow-list.** `git diff main -- apps/web-server/server/services/build_backend.py`
  is empty.

Follow-up issues to open after approval:

- ssh, pager and editor pins (plus E2 keys if declined)
- `--no-ext-diff` and `--no-textconv` on server diff calls
- a Write/Edit `.git/` guard once #1672 and #1673 settle their pattern
- `bash -c` unwrapping in `security/parser.py`
