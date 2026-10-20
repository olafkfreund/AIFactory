---
status: approved
issue: 1832
spec: spec/2026-10-20-1832-git-guard-followups.md
---

# Plan: close four gaps in the git metadata baseline guard, and test the QA checks

Paths are relative to the repo root; line numbers at `bea91102` (origin/dev).
`V=/mnt/code/Source-home/GitHub/AIFactory/apps/backend/.venv/bin`; put it
first on `PATH` (no `.venv` in the worktree). Run pytest from the repo root.

Approved decisions (from the spec):

- (2) Also track `lfs.customtransfer.*.path`, keys ending in
  `standalonetransferagent`, and `submodule.*.update` whose value starts
  with `!` (after leading whitespace).
- (6) The shared baseline (per common dir) holds only `local` scope keys.
  `worktree` scope keys go into a per-worktree file at
  `_baseline_path(gitdir + "\0worktree")`. A missing per-worktree file means
  `{}` expected (fail closed), and it is only written when non-empty. The check
  never writes it. `record_baseline` writes it under the same rule as the
  shared file (shared file new, or `overwrite`; with `overwrite` and empty it
  removes a stale file). New `record_worktree_baseline(cwd, run)` writes or
  removes only the per-worktree file; `create_worktree` calls it for the new
  worktree.
- (4) `clone_or_update` refuses a workspace name equal to `.git-baselines`
  with `InputRejectedError`.
- (5) `resolve_pr_conflicts` wraps the old body (renamed
  `_resolve_pr_conflicts`) and in a `finally` removes the shared baseline file
  only if it did not exist before the call. A path it cannot resolve means refuse.
- Two QA tests: post-loop check (`cli/build_commands.py:612`) and the check at
  the top of the pre-approved branch (`cli/build_commands.py:651`).
- Out of scope: items 1 and 3 (the other lane). Use `Refs #1832`, not `Closes`.

## Steps

1. `apps/backend/core/git_metadata.py:74-98` (item 2). Change
   `_is_tracked_config_key(key)` to `_is_tracked_config_key(key, value="")` and
   add three cases to the `or` chain:
   `(k.startswith("lfs.customtransfer.") and k.endswith(".path"))`,
   `k.endswith("standalonetransferagent")`,
   `(k.startswith("submodule.") and k.endswith(".update") and value.lstrip().startswith("!"))`.
   In `_parse_config` pass `value`. Tests in `tests/test_git_metadata_baseline.py`:
   one parametrized test that records a baseline, then sets each of
   `lfs.customtransfer.evil.path=/tmp/x`, `lfs.standalonetransferagent=evil`,
   `submodule.s.update=!touch x` with `git config`, and expects
   `RuntimeError` naming the key; and a test that `submodule.s.update=checkout`
   added after the baseline does NOT raise.
   → verify `$V/pytest tests/test_git_metadata_baseline.py -q`.
   Traps: values are never put in the message (only hashes are stored).

2. `apps/backend/core/git_metadata.py` (item 6) and `apps/backend/core/worktree.py:869-877`.
   - `_REV_PARSE_COMMON_DIR` becomes `git rev-parse --path-format=absolute
     --git-common-dir --git-dir` (rename the constant `_REV_PARSE_DIRS`). In
     `snapshot`, split the output lines: first is the common dir, second the git
     dir; when the second is missing (fake runners return ""), use the common
     dir. Resolve both like today (`(Path(cwd) / raw).resolve()`).
   - `_parse_config` returns two dicts: `local` and `worktree` scope entries
     (same filtering, same sorted shape). `snapshot` returns
     `{"repo", "gitdir", "config": local, "worktree_config": worktree, "attributes"}`.
   - Add `_worktree_baseline_path(gitdir) = _baseline_path(gitdir + "\0worktree")`
     and `_write_worktree_baseline(snap)`: write `{"config": snap["worktree_config"]}`
     with `_write_snapshot` when non-empty, else `unlink(missing_ok=True)`.
   - `record_baseline`: compute `fresh = not path.exists()`; if `overwrite or fresh`,
     write the shared file (the shared JSON keeps `repo`, `config`, `attributes`;
     do not write `worktree_config` or `gitdir` into it) and call
     `_write_worktree_baseline(snap)`.
   - New public `record_worktree_baseline(cwd, run=None)`: `snapshot`, then
     `_write_worktree_baseline`.
   - `check_git_metadata`: shared compare as today against `current["config"]`
     (local only). Keep the late-baseline branch exactly as is, but write only
     the shared fields, and also write the per-worktree file
     (`_write_worktree_baseline`), the same rule as `record_baseline` (a fresh
     shared file writes both). Deviation (implementation): without it a late
     baseline in a worktree with legit tracked `worktree` keys reports drift on
     every later check. Then load the per-worktree file if it exists (reuse the
     `config` dict check from `_load_baseline`, treat a non-dict as corrupt →
     RuntimeError), else `{}`; compare with `current["worktree_config"]` and add
     each differing key as `worktree:<key>` to `changed`. Keep the message format.
   - New public `baseline_file(cwd, run=None) -> Path`: run only the rev-parse,
     return `_baseline_path(commondir)` (used by step 3).
   - `worktree.py:870`: after `record_baseline(self.project_dir, run=...)`, inside
     the same `try`, call `record_worktree_baseline(worktree_path)` (real git in
     the new worktree; import it next to `record_baseline` at line 33).
   - The CLI `--rebaseline` path already calls `record_baseline(overwrite=True)`;
     no change.
   Tests in `tests/test_git_metadata_baseline.py`:
   (a) false positive: in `repo` set `extensions.worktreeConfig true`, add a
   linked worktree with `git worktree add`, then set
   `git config --worktree merge.x.driver true` in `repo` (after the add: git
   copies `config.worktree` into a new worktree, so before the add both sides
   match and old code passes), `record_baseline(repo)`, `record_worktree_baseline(wt)`, then
   `check_git_metadata(wt)` and `check_git_metadata(repo)` pass. Fails on old
   code: the main worktree's key is missing in the linked worktree's view.
   (b) real tamper: same setup, then `git config --worktree merge.y.driver evil`
   in the linked worktree → `check_git_metadata(wt)` raises naming `merge.y.driver`,
   and `check_git_metadata(repo)` still passes.
   (c) tamper in a worktree with no per-worktree file: enable worktreeConfig,
   baseline, `git config --worktree core.fsmonitor x` → raises.
   (d) `create_worktree` with worktreeConfig and a main `--worktree` tracked key
   → `check_git_metadata(wt.path)` passes.
   → verify `$V/pytest tests/test_git_metadata_baseline.py tests/test_worktree.py tests/test_merge_driver_pins.py tests/test_lfs_push.py tests/test_lfs_push_sites.py -q`.
   Traps: git 2.36+ copies `config.worktree` into a new linked worktree, so (d)
   needs `record_worktree_baseline`. Fake runners return "" for every argv; the
   two-line rev-parse parsing must cope. The baseline dir comes from
   `AIFACTORY_GIT_BASELINE_DIR` (set by `tests/conftest.py`).

3. `apps/web-server/server/services/pr_endgame.py:1249-1331` (item 5). Rename
   the body to `_resolve_pr_conflicts` (same signature and docstring).
   New `resolve_pr_conflicts` with the same signature:
   ```python
   run = lambda a: ((r := runner(a, worktree)).rc, r.out)
   try:
       baseline = baseline_file(worktree, run=run)
   except RuntimeError as exc:
       return ConflictResolution(False, [], f"rebase refused: {exc}"[:200])
   created = not baseline.exists()
   try:
       return _resolve_pr_conflicts(worktree, base_branch, fixer=fixer, runner=runner)
   finally:
       if created:
           baseline.unlink(missing_ok=True)
   ```
   Import `baseline_file` lazily from `core.git_metadata` like
   `merge_driver_pins` is imported in this module (check how; keep the module
   importable without the backend path). Use a `def` instead of a lambda if
   ruff flags E731. Tests in `tests/test_pr_conflict_resolution.py`:
   (a) with a real git repo clone and no baseline, `resolve_pr_conflicts`
   leaves no new file in `AIFACTORY_GIT_BASELINE_DIR`; (b) when the baseline
   existed before, it is still there after. (a) must fail on the old code.
   → verify `$V/pytest tests/test_pr_conflict_resolution.py tests/test_merge_driver_pins.py tests/test_git_metadata_baseline.py -q`.
   Traps: the fake runner returns "" for rev-parse, so `baseline_file` resolves
   `/wt`; that is fine. Do not delete a pre-existing baseline.

4. `apps/web-server/server/services/project_workspace_service.py:274` (item 4). Deviation: the check lives in a new `_workspace_name(git_url, slug)` helper that `clone_or_update` calls, because the extra branch tripped the CI ruff ratchet (PLR0912).
   Before building `workspace`, compute `name = slug or slug_from_git_url(git_url)`
   and raise `InputRejectedError("Invalid gitUrl: its path is not a usable
   workspace directory name")` when `name == ".git-baselines"` (one-line comment:
   the chart keeps the git baselines at `<workspace root>/.git-baselines`). Test in
   `tests/test_project_workspace_cloning.py`:
   `asyncio.run(clone_or_update("https://example.test/.git-baselines.git", root=tmp_path))`
   raises `InputRejectedError` and no directory was created.
   → verify `$V/pytest tests/test_project_workspace_cloning.py -q`.
   Traps: `slug_from_git_url("https://example.test/.git-baselines.git")` returns
   `.git-baselines` (check it; adjust the URL if not).

5. `tests/test_git_metadata_baseline.py` (QA tests). Copy the structure of
   `test_qa_fixer_metadata_change_fails_build_nonzero`:
   (a) post-loop: also patch `get_qa_iteration_count` in `loop_globals` to
   return `loop_globals["MAX_QA_ITERATIONS"]` so the while loop never runs; the
   human-feedback fixer plants `core.fsmonitor`; expect `SystemExit` code 1,
   `FAILED` in phases, no `COMPLETE`; assert the reviewer mock was not awaited.
   (b) pre-approved: patch `qa_loop.should_run_qa` to False and
   `qa_loop.is_qa_approved` to True (check the import name build_commands uses
   at line 641), tamper the config after `record_baseline` and before the call;
   expect exit 1 and `FAILED`.
   Prove each fails by deleting its call (`build_commands.py:612` or `:651`)
   in a scratch copy, then restore.
   → verify `$V/pytest tests/test_git_metadata_baseline.py -q`.
   Traps: other test files re-import `qa.loop`; patch the loop's `__globals__`
   as the existing test does.

6. `CHANGELOG.md` under `## [Unreleased]` → `### Security`: one entry for
   #1832 items 2, 4, 5, 6. → verify `ruff`/pre-commit pass.

## Tests

```
$V/pytest tests/test_git_metadata_baseline.py tests/test_pr_conflict_resolution.py \
  tests/test_merge_driver_pins.py tests/test_project_workspace_cloning.py \
  tests/test_worktree.py tests/test_lfs_push.py tests/test_lfs_push_sites.py -q
$V/ruff check <changed files>; $V/ruff format --check <changed files>
```
All green; each new test fails without its change.

## Rollback

Revert the PR. Per-worktree baseline files are only written for worktrees
with tracked `worktree`-scope keys and are ignored by the old code.
