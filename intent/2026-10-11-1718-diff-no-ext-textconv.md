---
status: draft
issue: 1718
author: olafkfreund
---

# Intent: server diffs must not run diff drivers or textconv from agent config

Follow-up to #1689 / PR #1722. That plan deferred this work to the current
issue (`plan/2026-10-10-1689-fsmonitor-git-config-audit.md`, Q3, and its
Rejected list).

Not fixed on dev. Checked at e176db99 (origin/dev, the base of this branch):
no file under `apps/` passes `--no-ext-diff` or `--no-textconv`, and no commit
or PR mentions #1718.

## Problem

An agent controls its worktree's `.gitattributes` and the project repo's
`.git/config`. A linked worktree shares that config, so whatever the agent
plants there stays after the task ends. A `diff=x` attribute together with
`diff.x.textconv=<prog>` or `diff.x.command=<prog>`, or a bare `diff.external`,
makes git run `<prog>` when it prints a patch.

The server prints patches inside agent worktrees, so `<prog>` runs as the
server process. This was reproduced with git 2.55.0 against the real code
paths. The `child_env()` pins from #1689 (`core.hooksPath`, `core.fsmonitor`,
`protocol.ext.allow`) do not stop it, and no git config key turns drivers or
textconv off. The exposed full-patch calls are:

- `apps/web-server/server/routes/worktree_merge.py:2416`: the per-file review
  diff. It runs every time a user opens a task's diff.
- `apps/backend/core/worktree.py:1006`: the branch diff fed to
  `security_pre_merge_gate_sync`. A planted textconv here can also rewrite the
  text that the gate scans.
- `apps/backend/analysis/insight_extractor.py:91`
- `apps/backend/runners/github/context_gatherer.py:559`
- `apps/backend/merge/file_evolution/modification_tracker.py:229`

Nothing visible happens when it fires. In one test the planted driver also
blanked the diff, so the server received "(Empty diff)".

These calls did not run either program in the test: `--stat`, `--numstat`,
`--shortstat`, `--name-only`, `diff-tree --stat`, and `git show ref:path`
blob reads. `git log -p` does run textconv, but no server code calls it.

A related gap: `_is_dangerous_git_config`
(`apps/backend/security/git_validators.py:31-58`) blocks `diff.external`,
`*command` and `*.driver`, but not `*.textconv`. So an agent can set a
textconv through the validated git tool itself, without editing the file by
hand.

## Proposed outcome

No server-side git call that prints patch content runs a diff driver,
`diff.external` or textconv program from repo config, whatever the agent
wrote into `.gitattributes` or `.git/config`. A regression test plants such
a program and shows that it does not run.

## Affected users and systems

- Operators of the control plane, the web-server and build Job pods. Today
  the planted program runs with their process's environment.
- Users who review task diffs in the UI.
- The pre-merge security gate (when `AIFACTORY_SELF_HEAL` is on).
- Developers who run `git diff` themselves in the project repo. The driver
  sits in the shared `.git/config`. This issue does not clean up a driver
  that is already there.

## Constraints

- Leave `filter.*` and `lfs.*` alone, in validators, pins and deny lists
  alike. #1690 owns them.
- Every call site must stay on `child_env()` / `_git_env()`.
  `tests/test_no_unscrubbed_spawn.py` must stay green.
- Diff output that code reads must not change, apart from losing textconv
  rendering.
- The test must run in CI with no network, against a real git.
- #1717 / PR #1748 (open) is editing `apps/backend/core/child_env.py`,
  `tests/test_child_process_env.py` and `tests/test_github_env.py`. Avoid
  those files, or wait for that PR to merge.
- #943 is out of scope.

## Open questions

1. Scope: only the five full-patch sites, or every diff-family call, with a
   scan test that enforces it? The stat-only calls did not run either program
   in the test.
2. Close the `*.textconv` gap in `_is_dangerous_git_config` here, or file it
   separately? The issue does not ask for it.
3. `git show ref:path` blob reads: leave them unchanged, or add
   `--no-textconv` as defense in depth?
4. Sequencing: wait for PR #1748 (#1717) to merge, or start now and stay out
   of `child_env.py`?
5. Diff views will show binary and LFS-pointer files raw instead of through
   textconv. Is that acceptable, or does any repo rely on textconv rendering?

## Approver decisions (2026-10-11)

- Add `--no-ext-diff --no-textconv` to the five full-patch diff sites, plus a scan test that requires them on every diff-family call.
- Close the `*.textconv` gap in `_is_dangerous_git_config` here.
- Leave `git show ref:path` blob reads unchanged.
- Start now and stay out of `child_env.py`.
- Binary/LFS-pointer files shown raw in diff views is acceptable.
