---
status: draft
issue: 1746
issues: [1746, 1747]
spec: spec/2026-10-17-1746-git-metadata-write-guards.md
---

# Plan: git-metadata baseline check before server merges (#1746, #1747, part A2)

Self-contained. You do not need to open the intent or the spec.

**Order.** Part A1 (`fix/1761-merge-driver-pins`, plan
`plan/2026-10-17-1761-merge-driver-pins.md`) merges first. This branch merges
A1's branch in before step 1, so `merge_driver_pins` exists here. Line numbers
below are on that merged tree.

**Problem.** An agent can write `.git/config` and `.git/info/attributes` with
Bash redirects, interpreters, or an external CLI (Codex, Antigravity, Copilot,
OpenCode) whose tool loop our hooks never see. The next server git command on
that repo can then run a planted program (merge drivers, filters, textconv,
and every key `_is_dangerous_git_config` names). A1 blanks merge drivers on
server merges; nothing notices the other keys or tells a human.

**Approved decisions** (user, 2026-10-17).

1. New module `apps/backend/core/git_metadata.py`:
   - `snapshot(cwd, run=None) -> dict[str, object]`:
     - `commondir`: `git rev-parse --path-format=absolute --git-common-dir`
       (resolve a relative answer against `cwd`).
     - `config`: from `git config --list --show-scope -z --includes`; the output
       is `scope\0key\nvalue\0` repeated (a key without value has no `\n`).
       Keep entries whose scope is `local` or `worktree` and whose lowercased
       key satisfies `_is_dangerous_git_config` (`security/git_validators.py:31`)
       or starts with `merge.`, `filter.`, `diff.`, or equals
       `core.attributesfile`. Map key → sorted list of SHA-256 hex of each
       value (values are never stored or logged).
     - `attributes`: SHA-256 hex of `<commondir>/info/attributes`, or `None`
       when absent.
     - `run(argv) -> (rc, stdout)` has the same meaning as in
       `merge_driver_pins`; the default runs `subprocess.run(argv, cwd=cwd,
       capture_output=True, text=True, errors="surrogateescape",
       env=child_env(), timeout=60, check=False)`. Any non-zero rc or
       `OSError`/`SubprocessError` raises `RuntimeError`.
   - Baselines live outside the repo, in `$AIFACTORY_GIT_BASELINE_DIR` or
     `~/.aifactory/git-baselines/`, one file per repo:
     `<sha256(commondir)[:32]>.json` = `{"repo": commondir, "config": ...,
     "attributes": ...}`, written via temp file + `os.replace`, mode 0600.
     One baseline per repo, not per task (worktrees share the commondir).
   - `record_baseline(cwd, *, overwrite=False, run=None) -> None`: writes the
     snapshot; with `overwrite=False` it keeps an existing file.
   - `check_git_metadata(cwd, run=None) -> None`: no baseline file → record it,
     log a warning "git metadata baseline taken late for <repo>", return
     (migration for tasks created before A2). Otherwise compare; on any
     difference raise `RuntimeError("git metadata changed since the baseline: "
     "<sorted key names, plus info/attributes>; check .git/config and "
     ".git/info/attributes, then re-baseline: python -m core.git_metadata "
     "--rebaseline <repo>")`. Key names only, never values. An unreadable or
     corrupt baseline file raises `RuntimeError` too (fail closed).
   - `if __name__ == "__main__"`: `--rebaseline <path>` calls
     `record_baseline(path, overwrite=True)` and prints the repo. This is the
     human re-baseline (user answer 2: refuse until a human re-baselines).
2. **Where it is checked.** `merge_driver_pins` (`core/child_env.py`, A1)
   calls `check_git_metadata(cwd, run)` before listing drivers (local import,
   to avoid a cycle). Every server merge, rebase and merge preview already calls
   `merge_driver_pins` inside a fail-closed `try` (A1, and the A1 static scan
   keeps it that way), so each of them now refuses on a change: previews report
   conflicts, merges and rebases are not started. No call site changes.
3. **Where it is recorded.** `WorktreeManager.create_worktree`
   (`core/worktree.py:796`), right after a successful `worktree add`
   (`:864`): `record_baseline(self.project_dir, run=...)` through `_run_git`
   (`run=lambda a: ((r := self._run_git(a[1:])).returncode, r.stdout)`).
   `RuntimeError` → `WorktreeError("could not record the git metadata
   baseline: ...")` (fail closed). It never overwrites: a second task on the
   same repo keeps the first baseline, so it cannot mask the first task's
   change.
4. **End of each session.** After each agent session returns:
   - serial coder `agents/coder.py:767` (`run_session_guarded`): call
     `check_git_metadata(project_dir)`; on `RuntimeError` print the message as
     an error, `status_manager.update(state=BuildState.ERROR)`, log it, and
     `break` out of the session loop (no retry: every retry would fail the
     same way);
   - parallel `agents/parallel_integration.py:591`: `check_git_metadata(child_path)`;
     on `RuntimeError` log it and set `session_ok = False`;
   - follow-up planner `agents/planner.py:133`: on `RuntimeError` set
     `status = "error"` (its existing path sets `BuildState.ERROR` and returns
     False).
5. Out of scope: an agent-side Bash write guard (user answer 3, Option B not
   built). A1's pins stay as they are: drivers present in the baseline are
   still blanked (user answer 1 accepts the preview/merge conflicts for now).
   Writes the agent makes to `~/.aifactory/git-baselines/` itself are outside
   this design (an agent with full host access can already do anything as the
   user; providers that sandbox to the project dir cannot reach it). k8s Job
   pods write their own `.git`, which the control plane never merges from
   locally; the server repo's baseline is taken on first check there.

## Steps

### Step 1: module and its tests

Files: new `apps/backend/core/git_metadata.py`; new
`tests/test_git_metadata_baseline.py`; `tests/conftest.py` and
`apps/web-server/tests/conftest.py` each get an autouse fixture
`_git_baseline_dir(tmp_path, monkeypatch)` setting
`AIFACTORY_GIT_BASELINE_DIR` to `tmp_path / "git-baselines"` (so no test
writes to the real home).

Change: decision 1 exactly. Read `AIFACTORY_GIT_BASELINE_DIR` on every call
(tests change it), create the dir with `mkdir(parents=True, exist_ok=True)`.

Tests:
- repo fixture (`git init -q -b main`, one commit); `record_baseline`, then
  `check_git_metadata` passes.
- appending `[merge "evil"]\n\tdriver = touch x\n` to `.git/config` with plain
  `open(..., "a")` (standing in for any provider) → `check_git_metadata`
  raises and the message names `merge.evil.driver` and not `touch x`.
- writing `.git/info/attributes` → raises naming `info/attributes`.
- `git config user.name other`, `git branch x`, creating and deleting
  `.git/index.lock` → still passes (not exec-bearing).
- `filter.lfs.clean` changed → raises.
- no baseline → `check_git_metadata` records it and passes; a second call
  compares.
- corrupt baseline file (`"{"`) → raises.
- `--rebaseline` via `subprocess.run([sys.executable, "-m",
  "core.git_metadata", "--rebaseline", repo], cwd=apps/backend, env with
  the baseline dir)` after a change → next check passes.
- a linked worktree (`git worktree add`) shares the baseline: change through
  the worktree path is seen from the main path.

Check: `pytest tests/test_git_metadata_baseline.py -q`; ruff check and format
on touched files.

Traps: `--show-scope` needs git 2.26, `--path-format` 2.31 (CI and images
ship newer). Section and variable names come lowercased from `--list`, the
subsection keeps its case; compare keys as given. `-z` splits records on
NUL: the stream is `scope, key\nvalue, scope, key\nvalue, ...`. Do not import
`security.git_validators` at module import time if it creates a cycle with
`core`; a local import inside `snapshot` is fine.

### Step 2: wire the check into merge_driver_pins and record at create_worktree

Files: `apps/backend/core/child_env.py` (`merge_driver_pins`, first line of
the body: `from core.git_metadata import check_git_metadata` then
`check_git_metadata(cwd, run)`; it raises `RuntimeError` like the rest, so
docstring: "Raises RuntimeError when git metadata changed since the baseline
(#1746)"), `apps/backend/core/worktree.py:864-871` (decision 3).

Tests (append to `tests/test_git_metadata_baseline.py`):
- `WorktreeManager(repo).create_worktree("t")` writes the baseline; a second
  `create_worktree("u")` after a planted change does not overwrite it.
- after baseline, plant `merge.evil.driver` + `info/attributes`:
  `merge_driver_pins(repo)` raises; `core.workspace.merge._check_git_conflicts`
  reports `has_conflicts`; `resolve_pr_conflicts` (real `_default_runner`) on a
  clone returns `resolved=False` with "refused" in the reason and the marker
  absent.
- an injected `run` returning `(0, "")` for everything (the
  `tests/test_pr_conflict_resolution.py` FakeRunner shape) still works: the
  first call records a baseline under the test dir and passes.

Check: `pytest tests/test_git_metadata_baseline.py tests/test_merge_driver_pins.py tests/test_pr_conflict_resolution.py tests/test_worktree.py tests/test_workspace.py tests/test_diff_no_ext_textconv.py -q`;
`cd apps/web-server && pytest tests/test_worktree_branch_as_truth.py tests/test_refused_merge_is_not_http_200.py tests/test_merge_worktree_logger.py -q`;
`pytest apps/backend/core -q -o asyncio_mode=auto`; ruff.

Traps: `create_worktree` tests elsewhere may mock `_run_git`; the extra
`rev-parse`/`config` calls go through it. If a mock returns a MagicMock
`stdout`, the snapshot fails closed and raises `WorktreeError`: script the
mock rather than catching broadly.

### Step 3: check at the end of each session

Files: `apps/backend/agents/coder.py` (after `:767-769`),
`apps/backend/agents/parallel_integration.py` (after `:591-594`),
`apps/backend/agents/planner.py` (after the `async with client:` block,
`:132-135`). Decision 4 exactly; import `check_git_metadata` from
`core.git_metadata`. Wrap the call in `await asyncio.to_thread(...)` in the
two async loops so git does not block the event loop.

Tests: one focused unit test per site is heavy (these loops need many
mocks); add one in `tests/test_git_metadata_baseline.py` for the planner
(the shortest path): patch `run_agent_session` to return
`("complete", "", {})` and plant a change before it returns; assert
`run_followup_planner` returns False. For coder and parallel, assert by
reading the source that the check follows the session call (a small AST/regex
test is enough) and rely on the step 2 runtime tests for the refusal logic.
Look at `tests/test_coder*.py` / `tests/test_parallel*.py` for existing
mocks first and prefer a real runtime test if one is cheap there.

Check: `pytest tests/test_git_metadata_baseline.py -q` plus the existing
coder/planner/parallel test files (`ls tests | grep -E "coder|planner|parallel"`);
`pytest apps/backend/agents -q -o asyncio_mode=auto`; ruff.

Traps: existing coder/parallel tests run sessions in tmp dirs that may not be
git repos: `check_git_metadata` then raises and the loop stops. If that
breaks them, patch `check_git_metadata` in those tests' fixtures rather than
making the check tolerant (fail closed).

## Verification

All checks above; each new runtime test fails on the A1 tree without this
change (prove it with a detached scratch worktree, never `git stash`).

## Rollback

Revert the PR. Baseline files under `~/.aifactory/git-baselines/` are inert
without the code and can be deleted.
