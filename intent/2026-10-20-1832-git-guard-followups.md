---
status: approved
issue: 1832
author: olafkfreund
---

# Intent: close four gaps in the git metadata baseline guard, and test the QA checks

## Problem

The #1818 review left non-blocking gaps in the git metadata baseline guard
(`apps/backend/core/git_metadata.py`). This lane takes items 2, 4, 5 and 6 of
#1832 plus the missing QA tests; another lane takes items 1 and 3.

- (2) The snapshot does not track `lfs.customtransfer.*.path`,
  `lfs.standalonetransferagent` (also `lfs.<url>.standalonetransferagent`) or
  `submodule.*.update` set to `!cmd`. Each names a program git or git-lfs runs,
  so a change to them is not reported.
- (4) On Kubernetes the baselines live at `<workspaces.mountPath>/.git-baselines`
  (`charts/aifactory/templates/deployment.yaml:586`), the same directory that
  holds project clones. A git URL whose slug is `.git-baselines` clones into
  the baselines directory.
- (5) `resolve_pr_conflicts` (`apps/web-server/server/services/pr_endgame.py:1249`)
  runs `merge_driver_pins`, which takes a late baseline for a repo that has
  none (a build clone). The baseline file stays on the PVC after the clone is gone.
- (6) With `extensions.worktreeConfig`, `git config --list --show-scope` shows
  the current worktree's `config.worktree` as scope `worktree`. The baseline is
  one file per common dir and is recorded from the main checkout, while checks
  run in task worktrees. A tracked key in one worktree's `config.worktree`
  therefore reads as drift in another. That is a false report, not a tamper.
- No test covers the post-loop QA check (`cli/build_commands.py:612`, after a
  last fixer session at max iterations) or the check at the top of the
  pre-approved QA branch (`cli/build_commands.py:638`).

## Proposed outcome

- A change to any of the three key families fails the check.
- A project slug of `.git-baselines` is refused.
- `resolve_pr_conflicts` removes a baseline file it created itself, and only that.
- Per-worktree config never causes drift in another worktree, and a change to
  a worktree's own `config.worktree` is still reported.
- Tests for each item fail without the change, plus the two QA tests.

## Affected users and systems

AIFactory backend (`core/git_metadata.py`, `core/worktree.py`) and web server
(`pr_endgame.py`, `project_workspace_service.py`). In-pod builds and the PR
endgame. No chart change.

## Constraints

- Fail closed: no change may hide a real tamper or turn a check into a pass.
- Smallest correct change. No deploys, no cluster writes.
- Items 1 and 3 belong to the other lane; do not touch them.

## Open questions

None. Conservative choices are recorded in the spec.
