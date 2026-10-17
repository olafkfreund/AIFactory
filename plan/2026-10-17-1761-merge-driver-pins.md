---
status: approved
issue: 1761
spec: https://github.com/olafkfreund/AIFactory/blob/docs/1746-git-metadata-write-guards/spec/2026-10-17-1746-git-metadata-write-guards.md
intent: https://github.com/olafkfreund/AIFactory/blob/docs/1746-git-metadata-write-guards/intent/2026-10-17-1746-git-metadata-write-guards.md
---

# Plan: pin merge drivers empty on server merges (#1761, part A1)

Self-contained. You do not need to open the intent or the spec (they live on
the design branch `docs/1746-git-metadata-write-guards`, PR #1818; part A2 of
the same spec lands there after this PR).

**Problem.** An agent can write `merge.<name>.driver = <program>` into the
repo's `.git/config` and `* merge=<name>` into `.git/info/attributes`. git then
runs that program on the server's `git merge-tree --write-tree`, `git merge`
and `git rebase` (verified with git 2.55). `--attr-source`, `attr.tree` and
`core.attributesFile=/dev/null` do not stop it (`info/attributes` is always
read). Overriding the driver with an empty value does: git reports a conflict
("cannot run") and runs nothing. The agent picks `<name>`, so the names must be
read from config at each call.

**Approved decisions** (user, 2026-10-17; fail closed everywhere).

1. New helper in `apps/backend/core/child_env.py`, after `pin_git_config`
   (`:51-66`):

   ```python
   _MERGE_DRIVER_KEYS = (
       "git", "config", "-z", "--includes", "--name-only", "--list",
   )

   def merge_driver_pins(
       cwd: str | os.PathLike[str],
       run: Callable[[list[str]], tuple[int, str]] | None = None,
       env: Mapping[str, str] | None = None,
   ) -> list[str]:
   ```

   Returns `["-c", "merge.<name>.driver=", ...]` for every driver key the
   config seen from `cwd` defines, to be placed right after `"git"` in an argv.
   `run(argv) -> (rc, stdout)` replaces the subprocess for callers with an
   injected runner; by default it runs `subprocess.run(argv, cwd=cwd,
   capture_output=True, text=True, errors="surrogateescape", env=env or child_env(),
   timeout=60, check=False)`. Any non-zero rc,
   `OSError`/`subprocess.SubprocessError`/`UnicodeError` from the run, or a
   driver key containing `=` (cannot be expressed with `-c`) raises
   `RuntimeError`. Keys are split on `"\0"` and kept only when they start with
   `merge.` and end with `.driver`.
   (Review fix: the first version used `--get-regexp '^merge\..*\.driver$'`
   and read rc 1 as "none". Under a UTF-8 locale git's `.` does not match an
   invalid byte, so a `[merge "\xff"]` driver was missed and ran. `--list`
   plus a Python filter has no regex, and rc 1 now fails closed. A custom
   `run` must decode with `surrogateescape` so such a key round-trips into
   argv.) Re-export it from
   `apps/web-server/server/utils/subprocess_env.py` like `pin_git_config`
   (alias line plus `__all__`).
   (Deviation from the spec text: the spec sketched `GIT_CONFIG_KEY_n` env
   pins and a `(key, value)` list. `-c` argv pins need no env plumbing and work
   through the injected `runner`/`_run_git` call shapes; same effect, git
   command scope.)
2. Every call site below computes `pins = merge_driver_pins(...)` in its own
   `try`/`except RuntimeError` and puts `*pins` right after `"git"`. On
   `RuntimeError`: a **preview** reports `has_conflicts = True`; a **real merge
   or rebase** is refused (not started) with an error naming the cause.
3. The two `merge-tree` calls with no `env=` also get `env=child_env()`.
4. Real user merge drivers no longer run on server merges either (user's
   choice; preview and merge show a conflict on files such a driver would
   resolve). Part A2 keeps them blanked too (its plan decision 5).
5. Out of scope: `git stash pop` after an aborted merge
   (`core/worktree.py:1110,1146`) and in `routes/pr.py` (merge machinery, but
   not listed by the spec; A2's baseline check covers them). Dynamic argv the
   scan cannot see stays the scan's documented limit.

## Steps

### Step 1 — helper and its tests

Files: `apps/backend/core/child_env.py` (after `:66`; `os`, `subprocess` and
`Callable` are already imported),
`apps/web-server/server/utils/subprocess_env.py` (next to
`pin_git_config = _core.pin_git_config`, and its `__all__`),
new `tests/test_merge_driver_pins.py`.

Change: add `_MERGE_DRIVER_KEYS` and `merge_driver_pins` exactly as decision 1,
with a docstring saying why (#1761) and that callers fail closed.

Tests (new file, fixture `evil_repo(tmp_path)` reused by later steps):
- repo with `user.email/name` set; `f` committed on `main`; branch `aifactory/t`
  changes `f` one way, `main` changes it another (a real content conflict);
  `git config merge.evil.driver "touch <marker>; exit 0"` (use an absolute
  marker path) and `.git/info/attributes` containing `* merge=evil`.
  Use `git init -q -b main`.
- control: plain `git merge-tree --write-tree main aifactory/t` creates the
  marker (else `pytest.skip`), then delete the marker.
- `merge_driver_pins(repo)` == `["-c", "merge.evil.driver="]`;
  `git *pins merge-tree --write-tree main aifactory/t` exits 1 and the marker
  is absent.
- the same repo with the bound driver named by the byte `0xff` (appended raw
  to `.git/config`, `* merge=\xff` in `info/attributes`, `LC_ALL=C.UTF-8`):
  the key `merge.\udcff.driver=` is pinned and merge-tree runs nothing; and
  `WorktreeManager._do_merge_locked` returns False with no marker.
- injected `run`: `(0, "")` → `[]`; `(0, "core.bare\0merge.a.driver\0merge.B c.driver\0merge.a.name\0")`
  → the two drivers pinned in order; `(1, "")` and `(128, "")` raise `RuntimeError`;
  `(0, "merge.a=b.driver\0")` raises; a `run` that raises `OSError` →
  `RuntimeError`.

Check: `cd apps/backend && .venv/bin/python -m pytest ../../tests/test_merge_driver_pins.py -q`
(or from repo root `pytest tests/test_merge_driver_pins.py -q` with the backend
venv on PATH), then `ruff check` and `ruff format --check` on the touched files.

Traps: `-z` with `--name-only` ends each key with NUL, no newline. Subsection
names keep their case (`merge.B c.driver`); do not lowercase. Do not catch
`Exception` broadly; only `OSError`, `subprocess.SubprocessError`.

### Step 2 — the three merge-tree previews

1. `apps/web-server/server/routes/worktree_merge.py:221-256`
   (`get_worktree_merge_preview`): inside the existing `try`, before the
   `subprocess.run`, `pins = merge_driver_pins(project_path)`; argv becomes
   `["git", *pins, "merge-tree", "--write-tree", base_branch, work_ref]`.
   Add `except RuntimeError:` to that `try`: log a warning
   ("merge preview refused: cannot read merge drivers") and set
   `has_conflicts = True`. Import `merge_driver_pins` from
   `server.utils.subprocess_env` (`:55`).
2. `apps/backend/core/workspace/merge.py:615-630` (`_check_git_conflicts`):
   before the `merge-tree` run:
   ```python
   try:
       pins = merge_driver_pins(project_dir)
   except RuntimeError as exc:
       debug_warning(MODULE, f"Cannot read merge drivers: {exc}")
       result["has_conflicts"] = True
       return result
   ```
   argv `["git", *pins, "merge-tree", ...]`, add `env=child_env()`. Import
   `from core.child_env import child_env, merge_driver_pins` at module top.
3. `apps/backend/cli/workspace_commands.py:505-520`
   (`_check_git_merge_conflicts`): same as 2 (`debug_warning` or `debug`,
   whichever the module imports); extend the existing
   `from core.child_env import (child_env,)` at `:12`.

Tests (append to `tests/test_merge_driver_pins.py`): with `evil_repo` checked
out on `main`, `_check_git_conflicts(repo, "t")` and
`_check_git_merge_conflicts(repo, "t")` return `has_conflicts is True` and the
marker is absent. Monkeypatch `merge_driver_pins` in each module to raise
`RuntimeError` → `has_conflicts is True`. Import paths: see how
`tests/test_workspace.py` imports `core.workspace.merge`, and
`cli.workspace_commands` with `apps/backend` on `sys.path`.

Check: `pytest tests/test_merge_driver_pins.py tests/test_workspace.py -q`,
`cd apps/web-server && pytest tests/test_worktree_branch_as_truth.py tests/test_route_refusals_are_not_http_200.py -q`, ruff on touched files.

Traps: these previews run git in `project_dir`, not the worktree; pin from the
same cwd. The pins stop the driver; `env=child_env()` adds the hooks and
fsmonitor pins those two calls lack today. Add both.
Existing tests may patch `subprocess.run` with scripted results in call order;
the new `git config` call is an extra call; if one breaks, script it rather
than reorder the code.

### Step 3 — the real merge and rebase sites

1. `worktree_merge.py:655-661` (`resolve_worktree_conflicts`,
   `git merge work_ref --no-commit --no-ff`): compute pins first; on
   `RuntimeError` return
   `{"success": False, "error": "Merge refused: cannot read merge drivers (#1761)"}`
   without starting the merge. argv `["git", *pins, "merge", ...]`.
2. `worktree_merge.py:1947-1958` (`merge_worktree`, `merge_cmd`): same; it is
   inside a `try` whose excepts start near `:1985`/`:2000`; add the pins call
   before `merge_cmd` and an early `return {"success": False, "error": ...}`
   on `RuntimeError` (keep the existing excepts unchanged).
3. `apps/backend/core/worktree.py:1057` (`_do_merge_locked`): at the top of
   the method, before the stash (`:1083`):
   ```python
   try:
       pins = merge_driver_pins(self.project_dir, env=_git_env())
   except RuntimeError as exc:
       print(f"Error: merge refused: {exc}")
       logger.error("Merge refused for '%s': cannot read merge drivers", spec_name)
       return False
   ```
   and `merge_args = [*pins, "merge", "--no-ff", info.branch]` (`:1121`).
   Not `_run_git`: it decodes with `errors="replace"`, which turns a
   non-UTF-8 driver name into U+FFFD so the pin misses (review fix).
   `env=_git_env()` keeps ambient `GIT_DIR` dropped.
4. `apps/web-server/server/routes/pr.py:270-298` (`create_pr_from_task`,
   best-effort rebase): inside the `try`, `pins = merge_driver_pins(worktree_path)`
   then `["git", *pins, "rebase", f"origin/{base_branch}"]`. Add
   `except RuntimeError: rebase_failed = True` (nothing started, nothing to
   abort). Import from `server.utils.subprocess_env` (`:40`).
5. `apps/web-server/server/services/pr_endgame.py:1157-1189`
   (`resolve_pr_conflicts`): after `base_ref`,
   ```python
   try:
       pins = merge_driver_pins(
           worktree, run=lambda a: ((r := runner(a, worktree)).rc, r.out)
       )
   except RuntimeError as exc:
       return ConflictResolution(False, [], f"rebase refused: {exc}"[:200])
   ```
   then `["git", *pins, "rebase", base_ref]` and
   `["git", *pins, "-c", "core.editor=true", "rebase", "--continue"]`
   (`--continue` replays further commits, so it merges too). Import from
   `server.utils.subprocess_env` (`:36`).

Tests (append): `resolve_pr_conflicts` with the real `_default_runner` on a
clone of `evil_repo` (`git clone`, then check out `aifactory/t`, plant the
driver config and `info/attributes` in the clone) and a fixer that records
its call and returns False: the marker is absent and the fixer saw `["f"]`.
The `FakeRunner` tests in `tests/test_pr_conflict_resolution.py` must still
pass unchanged (an unscripted `git config` returns rc 0 and empty out → no
pins).

Check: `pytest tests/test_merge_driver_pins.py tests/test_pr_conflict_resolution.py tests/test_worktree.py tests/test_worktree_merge_dirty.py tests/test_create_pr_fetches_branch.py -q`,
`cd apps/web-server && pytest tests/test_refused_merge_is_not_http_200.py tests/test_merge_worktree_logger.py tests/test_approve_existing_pr.py tests/test_resolve_conflicts_reads_the_commit.py -q`, ruff.

Traps: do not pin `merge --abort`, `rebase --abort` or `stash` calls. Keep
`check=True` and every existing except in `merge_worktree`. Mocks of
`subprocess.run` that assert exact argv for `git merge`/`git rebase` will see
the same argv when the repo has no drivers (pins `[]`), but they also see an
extra `git config` call.

### Step 4 — widen the static scan (#1718, #1761 item 2)

File: `tests/test_diff_no_ext_textconv.py`.

1. Diff family (`:22-25`, `_is_diff_family`, `_git_diff_calls`):
   - `show` counts unless `--no-patch` or `-s` is present or every positional
     argument after `show` is a blob read: a `str` constant containing `:` or
     an f-string (`ast.JoinedStr`) with a constant part containing `:`. A
     positional that is neither (a name, a call) counts as not a blob read.
     `show -p`/`--patch` still counts.
   - `blame` always counts and needs only `--no-textconv`.
   - `range-diff` always counts.
   - `stash` counts when its constants include `show` and `-p`/`--patch`.
   - Required flags for everything except `blame` stay `_REQUIRED`.
   `_is_diff_family` needs the AST elements for the blob check: pass `elts`
   alongside `vals`.
   Split the per-file walk into `_calls_in(tree, rel)` so tests can scan a
   source string.
2. New `test_scan_flags_planted_calls`: `_calls_in(ast.parse(src), "x.py")` on
   `subprocess.run(["git", "show", "HEAD"])`, `["git", "blame", "f"]`,
   `["git", "range-diff", "a...b"]`, `["git", "stash", "show", "-p"]` each yields
   one offending call; `["git", "show", f"{r}:{p}"]`,
   `["git", "show", "--no-patch", "HEAD"]` yield none.
3. New `test_every_server_merge_pins_merge_drivers`: every literal argv in
   `apps/` (same `_sources()`) whose first constant is `"git"` and whose
   constants contain `merge-tree`, or `merge`/`rebase` without `--abort`,
   must contain a `Starred` element whose value is either a call to
   `merge_driver_pins` or a `Name` that the same file assigns from a
   `merge_driver_pins(...)` call. Plus a planted-source self-check: a source
   with `["git", "merge", b]` and no pins is flagged; one with
   `pins = merge_driver_pins(c)` and `["git", *pins, "merge", b]` is not.
   Known limit (docstring): argv without a literal `"git"`
   (`core/worktree.py` `merge_args`, run through `_run_git`) is not seen; the
   runtime test covers... no runtime test covers it, so name it in the
   docstring.

Check: `pytest tests/test_diff_no_ext_textconv.py -q` passes on the branch
(no current call offends: every `show` today is a `ref:path` read). Revert
step 2/3 temporarily (scratch copy, never `git stash`) and the merge check
must fail.

Traps: `_SUMMARY`/`_FULL_PATCH_FILES` behaviour must not change.
`apps/backend/spec/complexity.py` has `"show"` in a keyword list; it has no
`"git"` and no git callee, so it must stay unmatched. `context_gatherer.py`
passes `"git", "show", f"{ref}:{path}"` as call args (`asyncio
create_subprocess_exec`): the blob rule must cover call-argument form too.

## Verification

All checks above, plus `ruff check apps tests/test_merge_driver_pins.py
tests/test_diff_no_ext_textconv.py` and `ruff format --check` on touched files.
Each new runtime test fails on `origin/dev` (prove with a detached scratch
worktree).

## Rollback

Revert the PR. No data, config or schema changes.
