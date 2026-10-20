---
status: approved
issue: 1832
author: olafkfreund
---

# Intent: pin filters on server status/add/diff, and make a late baseline visible (#1832 items 1 and 3)

## Problem

Two of the follow-ups the #1818 review left in #1832:

- **Item 3, server-side filter execution.** An agent can define
  `filter.<x>.clean`, `.smudge` or `.process` in `.git/config` and bind it with
  `* filter=x` in `.git/info/attributes` or `.gitattributes`. When the server
  later runs `git status`, `git add` or a worktree `git diff` on that repo, git
  runs the planted program in the server process. #1761/#1818 pinned only
  merge drivers (`merge_driver_pins`, `apps/backend/core/child_env.py:79`), and
  only on merges, rebases and previews. Checked on git 2.54: `status
  --porcelain`, `add <path>`, `diff --name-only` and `diff --diff-filter=U`
  all run the clean filter; `worktree remove --force` does not.
- **Item 1, late baseline passes silently.** `check_git_metadata`
  (`apps/backend/core/git_metadata.py:192`) writes a baseline and returns when
  none exists, with only a `logger.warning`. The baseline is recorded only in
  `WorktreeManager.create_worktree` (`core/worktree.py:869`). Spec-pipeline
  agents run before that, and direct/CLI-only builds never call it, so a change
  made before the late baseline is never seen and nobody is told.

## Proposed outcome

- Every server-side `git status`, `git add` and worktree-reading `git diff` in
  the server git paths runs with every configured filter's `clean`, `smudge`
  and `process` pinned empty, so a planted filter does not run.
- The baseline is taken before the spec pipeline's agents run and before the
  build's first agent session, in direct mode too.
- When a check still has to take a late baseline, the build output says so
  plainly, not only the logger.

## Affected users and systems

`apps/backend/core/child_env.py`, `core/git_metadata.py`, `core/worktree.py`,
`core/workspace/{merge,git_utils}.py`, `cli/build_commands.py`,
`runners/spec_runner.py`, web-server `routes/worktree_merge.py`,
`routes/git.py`, `services/pr_endgame.py`. Control-plane pod and build Jobs.

## Constraints

- Reuse `merge_driver_pins`; no second helper.
- Do NOT make the late-baseline path fail closed: direct/CLI-only mode depends
  on it, and that is a design decision the user owns.
- Fail closed where pins cannot be read (same as merges).
- Items 2, 4, 5 and 6 of #1832 are out of scope (another lane).

## Open questions

- Should a missing baseline fail closed instead of warning? Left to the user
  (recorded in the spec).
