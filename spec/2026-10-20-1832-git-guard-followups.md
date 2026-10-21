---
status: approved
issue: 1832
intent: intent/2026-10-20-1832-git-guard-followups.md
---

# Spec: close four gaps in the git metadata baseline guard, and test the QA checks

## Design

**(2) Untracked keys.** `_is_tracked_config_key` in
`apps/backend/core/git_metadata.py:74` gains a `value` argument. Also tracked:
keys that start with `lfs.customtransfer.` and end with `.path`; keys that end
with `standalonetransferagent` (covers `lfs.standalonetransferagent` and
`lfs.<url>.standalonetransferagent`); and `submodule.*.update` when the value,
after leading whitespace, starts with `!`. `_parse_config` passes the value.
A `submodule.x.update` that changes from `!cmd` to `checkout` drops the key,
which also counts as drift (fail closed).

**(6) Per-worktree config.** The shared baseline (one file per common dir)
keeps only `local` scope keys. Scope `worktree` keys go into a separate
per-worktree baseline, keyed by the worktree's absolute git dir:
`_baseline_path(gitdir + "\0worktree")`. A missing per-worktree file means
"no tracked keys expected", so a key planted in a worktree that had none is
drift (fail closed), and no file is written for the usual empty case (no PVC
clutter). `snapshot()` gets the git dir from the same `rev-parse` call
(`--git-common-dir --git-dir`, two lines) and returns it plus `worktree_config`.

- `record_baseline(cwd, overwrite)` writes the per-worktree file under the
  same rule as the shared one (only when the shared file is new, or
  `overwrite`). With `overwrite` and no worktree keys it removes a stale file.
- New `record_worktree_baseline(cwd, run)` writes (or removes, when empty)
  only the per-worktree file. `WorktreeManager.create_worktree`
  (`apps/backend/core/worktree.py:870`) calls it for the new worktree right
  after `record_baseline(project_dir)`: git copies the main worktree's
  `config.worktree` into a new linked worktree, and that copy is what we just
  created, not a tamper.
- `check_git_metadata` compares local keys with the shared file and the
  worktree keys with the per-worktree file (missing means `{}`). It never
  writes the per-worktree file.

Old baseline files that mixed `worktree` keys into `config` report drift once
in a repo that has tracked worktree keys; the message already names the
rebaseline command, and the CLI rebaseline rewrites both files.

**(4) Reserved slug.** `clone_or_update` in
`apps/web-server/server/services/project_workspace_service.py:274` refuses
(`InputRejectedError`) a workspace directory name equal to `.git-baselines`,
whether it came from the URL or the `slug` override. Refusing is the smallest
fix and keeps every existing PVC layout.

**(5) Temp clone leftovers.** `resolve_pr_conflicts` becomes a thin wrapper:
before it calls the existing body (renamed `_resolve_pr_conflicts`), it asks
the new `git_metadata.baseline_file(cwd, run)` for the shared baseline path
and notes whether it exists. In a `finally`, it removes the file only if the
call created it. A baseline that existed before (the main repo's, for a linked
task worktree) is never removed, so protection for real repos is unchanged.
If the path cannot be resolved, the wrapper refuses like the body does
(`ConflictResolution(False, [], "rebase refused: ...")`).

**QA tests.** Two tests in `tests/test_git_metadata_baseline.py`, built like
`test_qa_fixer_metadata_change_fails_build_nonzero`:

- post-loop: the QA iteration count is already at `MAX_QA_ITERATIONS` and a
  `QA_FIX_REQUEST.md` exists, so the loop runs only the human-feedback fixer
  (which plants `core.fsmonitor`) and never reaches the in-loop check. The
  build must exit 1 with `FAILED` and no `COMPLETE`.
- pre-approved: `should_run_qa` is false and `is_qa_approved` is true, and the
  config was tampered after the baseline. The build must exit 1 with `FAILED`.

Each test must fail when its call site in `cli/build_commands.py` is removed.

## Alternatives rejected

- Hash every `config.worktree` under the common dir in one baseline: removing
  or adding a task worktree changes the set and reports drift.
- Drop `worktree` scope from the snapshot: hides a real tamper of `config.worktree`.
- Late-record a per-worktree baseline in `check_git_metadata`: a tamper made
  before the first check would be accepted silently (the item-1 problem).
- Namespace the baselines dir in the chart: moves existing baselines on live
  PVCs; a reject is smaller.
- Delete the baseline at the end of every `resolve_pr_conflicts`: for a linked
  task worktree that is the main repo's baseline, and deleting it would make
  the next check accept a tamper.
- Track every `submodule.*.update`: false drift for benign mode changes.

## Risks

- One-time drift for repos with tracked `worktree`-scope keys and an
  old-format baseline (rare; the message names the fix).
- A tamper of the main worktree's `config.worktree` before a new linked
  worktree is created is copied into the new worktree's baseline. An agent
  cannot write the main worktree's `config.worktree` from a task worktree
  (`git config --worktree` writes its own), and `.git` writes are refused.
- Merge conflicts in `git_metadata.py` with the item-1 lane; resolve on merge.

## Verification

- New tests in `tests/test_git_metadata_baseline.py`,
  `tests/test_pr_conflict_resolution.py` and the workspace service tests, each
  shown to fail on the unfixed code.
- Existing `tests/test_git_metadata_baseline.py`, `tests/test_merge_driver_pins.py`,
  `tests/test_pr_conflict_resolution.py`, `tests/test_worktree.py` and
  `tests/test_lfs_push*.py` stay green.
- `ruff check`, `ruff format --check`, and CI.
