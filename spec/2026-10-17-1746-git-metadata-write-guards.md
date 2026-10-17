---
status: approved
issue: 1746
issues: [1746, 1747, 1761]
intent: intent/2026-10-17-1746-git-metadata-write-guards.md
---

# Spec: git metadata write guards (#1746, #1747, #1761)

The user chose Option A on 2026-10-17. Options B and C are kept below for
the record, marked not chosen.

## Decisions (user, 2026-10-17)

- **Option A, in two ordered PRs.**
- **A1 first**, its own small PR on `fix/1761-merge-driver-pins` from
  `origin/dev`, with its own plan
  (`plan/2026-10-17-1761-merge-driver-pins.md`), sharing this intent and spec.
  It adds `merge_driver_pins(cwd)` next to `pin_git_config` in
  `core/child_env.py`, which pins every `merge.*.driver` to an empty value. It
  applies the pins to the three `merge-tree` previews **and to the real server
  merge and rebase sites listed under A2** (wider than A1 below, which left real
  merges alone). It adds `child_env()` to the two `merge-tree` calls with no
  `env=`. If the helper fails, a preview reports conflicts and a real
  merge/rebase is refused (fail closed). It widens the #1718 scan. Closes #1761.
- **A2 second**, on this branch: record the baseline of exec-bearing git config
  keys and a hash of `info/attributes` when the worktree is created; check it
  before every server merge, rebase or preview and at the end of each session;
  on any change refuse the operation until a human re-baselines. Closes #1746
  and #1747.
- Answer 1: for now it is acceptable that previews (and, with A1 as chosen,
  real merges) in repos with real merge drivers show conflicts a driver would
  resolve. A2 lets drivers present in the baseline run again.
- Answer 2: refuse the merge until a human re-baselines (no blanking and
  continuing).
- Answer 3: no agent-side Bash write guard; #1746 and #1747 close with A2.
  Option B is not built.

## Facts the options rest on

Verified on `origin/dev` 4638e87d with git 2.55, in a scratch repo:

| Setup | `merge-tree --write-tree` | real `git merge` |
| --- | --- | --- |
| `merge.evil.driver` in repo config + `* merge=evil` in `.git/info/attributes` | runs driver | runs driver |
| same, `.gitattributes` committed only on the agent branch | no | no |
| same as row 1, `--attr-source=<empty tree>` or `-c attr.tree=<empty tree>` | runs driver | — |
| same as row 1, `-c core.attributesFile=/dev/null` | runs driver | — |
| same as row 1, `-c merge.evil.driver=` (empty) | **no** (reports a conflict, "cannot run") | no |

So both halves of the attack live in the shared git dir (`<repo>/.git/config`
and `<repo>/.git/info/attributes`; a linked worktree shares them through its
commondir). The only config-level way to stop a driver is to override each
driver name, and the names come from the agent.

Textconv: `git show <commit>` and `git blame <file>` run `diff.<x>.textconv`
by default; `git show <ref>:<path>`, `git show --no-patch` and
`git format-patch` do not.

## Option A (chosen): server-side, in two independent steps

**A1. Merge previews never run merge drivers (#1761 item 1).** Ship first; no
dependency on anything else.

- Add `merge_driver_pins(cwd) -> list[tuple[str, str]]` next to
  `pin_git_config` in `apps/backend/core/child_env.py:51`. It runs
  `git config --includes --get-regexp '^merge\..*\.driver$'` under
  `child_env()` and returns `(f"merge.{name}.driver", "")` for every name.
  Exit 1 (no match) returns `[]`. Any other failure raises, and the caller
  treats the preview as "conflicts unknown" (`has_conflicts=True`), which is
  fail-closed.
- Append those pins as `GIT_CONFIG_KEY_n`/`VALUE_n` after the `child_env` pins
  at the three `merge-tree` sites:
  `apps/web-server/server/routes/worktree_merge.py:224`,
  `apps/backend/core/workspace/merge.py:620`,
  `apps/backend/cli/workspace_commands.py:511`. The last two pass no `env=`
  today; they get `env=child_env()` as well (hooks and fsmonitor pins).
- Widen the #1718 scan, `tests/test_diff_no_ext_textconv.py:22-25`
  (#1761 item 2): count `show` unless every positional argument is a
  `ref:path` blob read or `--no-patch`/`-s` is present; count `stash show -p`,
  `blame` and `range-diff`. Leave `format-patch` out (verified: no textconv).
  Add a second check in the same file: every literal `merge-tree` argv passes an
  `env=` built with `merge_driver_pins`.
- #1761 item 3 (dynamic argv invisible to the scan): accepted as the scan's
  documented limit (`tests/test_diff_no_ext_textconv.py:79-80`); A2 covers the
  runtime side for merges.

What breaks: in a repo with a real merge driver, the preview reports a conflict
on files that driver would resolve. The real merge is unchanged and still runs
the driver. No other behaviour changes.

**A2. Git-metadata baseline check before server merges (#1746, #1747, rest of
#1761 item 1).**

- When the server creates a task worktree (`WorktreeManager`,
  `apps/backend/core/worktree.py`), record a baseline of the repo's
  exec-bearing git metadata: every key from
  `git config --includes --list` (local and worktree scopes only) that
  `_is_dangerous_git_config` (`security/git_validators.py:31`) matches, plus
  `merge.*`, `filter.*`, `diff.*`, `core.attributesfile`, and the SHA-256 of
  `<commondir>/info/attributes`. Store it beside the existing per-task worktree
  state.
- Before every server merge, rebase and merge preview on that repo
  (`worktree_merge.py:224`, `:656`, `:1948`, `core/worktree.py:1121`,
  `server/routes/pr.py:273`, `server/services/pr_endgame.py:1158`) and at the
  end of each agent session, recompute and compare. On a difference, refuse the
  operation with "git metadata changed during the build: <key names>" and log
  the keys (not values). A human re-baselines after checking.
- Real merge drivers that were present at baseline time keep running on real
  merges. Once A2 exists, A1 can blank only drivers missing from the baseline,
  which removes A1's preview false conflicts.

Why this closes #1746 and #1747: the check is at the point where the planted
config would execute, so it does not matter whether Bash, `python -c`, Codex,
Antigravity, Copilot, OpenCode or an MCP tool wrote the file. `.git/index.lock`,
objects, refs and the index are not part of the baseline, so lock cleanup and
`git commit` keep working.

What breaks: a user who edits repo-local exec config (adds a merge driver,
changes `info/attributes`) while a build is running gets a refused merge until
they re-baseline. Agents that run `git config` for a merge/filter/diff key get
caught (they are already refused on the command line).

Migration: tasks created before A2 have no baseline. Fail-closed choice: take
the baseline at the first check and log that it was taken late (the alternative,
refusing, would block every in-flight task once).

Test plan:
- A1: a pytest that builds a repo with `merge.evil.driver="touch <marker>"` and
  `* merge=evil` in `info/attributes`, calls each preview helper, and asserts the
  marker is absent and `has_conflicts` is true; fails on current `dev`.
  The widened scan must pass on `dev` (no offending call exists) and fail on a
  planted `["git", "show", "HEAD"]` fixture string.
- A2: same repo fixture; baseline, then append a driver to `.git/config` from a
  plain `open()` (standing in for any provider); assert the merge route refuses
  and the marker is absent. Assert a driver present at baseline still runs on a
  real merge. Assert `rm .git/index.lock` between baseline and check does not
  trip it.

## Option B (not chosen): agent-side guards (the issues as written)

- #1746: extend `_collect_command_nodes` (`security/ast_parser.py:364`) to
  collect redirect targets, and refuse a `.git` component (via
  `git_metadata_write_refusal`) in redirect targets and in destinations of
  `tee`, `cp`, `mv`, `ln`, `install`, `sed -i`, `dd of=`, `truncate`; allow
  `rm` of `index.lock` only.
- #1747: switch Codex to `sandbox: workspace-write` and rely on its protected
  `.git` (unverified for linked worktrees); nothing exists to configure for
  Antigravity `--yolo`, Copilot or OpenCode.

What breaks: little for Claude agents. Codex's sandbox may stop it committing.
Why not recommended: it does not close the hole. Interpreters (`python -c`,
`node -e`), scripts the agent wrote, `make`/`npm` targets and three of four
external CLIs still write `.git`. It is a denylist on a parser the accepted
limits already say has holes. It could ship later as early feedback to the
agent, on top of A.

## Option C (not chosen): OS-level read-only git metadata

Bind `<commondir>/config`, `<commondir>/info/` and `<commondir>/hooks/`
read-only into every agent process: bwrap `--ro-bind` for local runs, a
read-only `subPath` mount in `core/kube_sandbox.py:155-170` for Job pods, and a
wrapper around each external CLI.

What breaks: agent `git config` writes, `git submodule`/`git worktree`
operations that write config; bwrap is unavailable in pods
(`core/client.py:744-760`), so pods need their own mount layout; every provider
spawn path changes. Closes everything at the source, but it is the largest
change and needs a runtime matrix test. A long-term option, not a first step.

## Options summary

| | Closes Bash/interpreters | Closes external CLIs/MCP | Real drivers keep working | Size |
| --- | --- | --- | --- | --- |
| A1 only | merge-tree only | merge-tree only | yes (preview more conservative) | small |
| **A1 + A2** | yes (at point of use) | yes | yes | medium |
| B | partly | no (Codex maybe) | yes | medium |
| C | yes | yes | yes | large |

## Alternatives rejected

- `--attr-source`, `attr.tree`, `core.attributesFile=/dev/null`: do not cover
  `.git/info/attributes` (verified above).
- Blanking every merge driver on real merges too: breaks the user's real
  drivers (constraint).
- Post-run full-file hash of `.git/config`: trips on harmless writes
  (`user.name`, `branch.*`, `remote.*` from `git worktree`/`push -u`).
- A per-key denylist scan without a baseline: a denylist on its own, which the
  #1719 spec rejected; the baseline diff only needs the key families to be
  coarse.

## Risks

- A2 baseline scope: user-global and system config are outside it (server-owned,
  trusted). `include.path` targets are read through `--includes`.
- Concurrent tasks on one repo share the commondir; one baseline per repo,
  not per task, or one task's legitimate re-baseline masks another's change.
- A1 adds one `git config` subprocess per preview.

## Verification

The test plans under A1 and A2, plus `ruff check`, `ruff format --check`, and
`pytest tests/test_diff_no_ext_textconv.py tests/test_command_sandbox.py`.
