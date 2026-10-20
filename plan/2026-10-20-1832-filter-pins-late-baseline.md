---
status: draft
issue: 1832
spec: spec/2026-10-20-1832-filter-pins-late-baseline.md
---

# Plan: pin filters on server status/add/diff, and make a late baseline visible (#1832 items 1 and 3)

## Decisions (carried from the approved spec)

1. Reuse `merge_driver_pins` (`apps/backend/core/child_env.py:79`): besides
   `merge.<x>.driver`, return `-c <key>=` for every listed key that starts with
   `filter.` and ends with `.clean`, `.smudge` or `.process`. Same `=` refusal,
   same baseline check first, same name and signature. No new helper.
2. Do not pin `filter.<x>.required`; with it set git exits 128 (fail closed).
3. Pin every server-side `git status`, `git add` and worktree-reading `git diff`
   (no revision args, no `--cached`/`--staged`) at the sites in the table below.
   Reuse a `pins` the function already computed for the same repo; otherwise
   call `merge_driver_pins(<cwd>)` once per function and on `RuntimeError` take
   the site's existing failure path. A status probe must never report "clean"
   when pins cannot be read.
4. `git worktree remove --force` runs no filter (git 2.54): no change; the scan
   requires `--force` or pins on it.
5. Record the baseline (never overwrite) before the spec pipeline's agents
   (`runners/spec_runner.py`) and before the build's first agent session
   (`cli/build_commands.py`), best-effort: `RuntimeError` is logged and ignored.
6. `check_git_metadata` still takes a late baseline and passes (NOT fail closed;
   the user owns that decision), but also prints a `WARNING:` line to stdout.
7. Out of scope: #1832 items 2, 4, 5, 6; `stash`/`checkout` sites.

Sites (line numbers on origin/dev at bea91102):

| File | Lines | Function | Pins source |
| --- | --- | --- | --- |
| `apps/backend/core/worktree.py` | 1107 | `_do_merge_locked` | existing `pins` (1074) |
| `apps/backend/core/worktree.py` | 1205 | `commit_in_worktree` | new; on error return False |
| `apps/backend/core/worktree.py` | 1408 | `has_uncommitted_changes` | new; on error return True |
| `apps/backend/core/workspace/git_utils.py` | 200 | `has_uncommitted_changes` | new; on error return True |
| `apps/backend/core/workspace/merge.py` | 826, 947, 957, 1008, 1128, 1162, 1176 | `_resolve_git_conflicts_with_ai` | new, once at top; on error return the function's failure result |
| `apps/web-server/server/routes/worktree_merge.py` | 327 | `get_worktree_merge_preview` | existing `pins` (222) if in scope, else new |
| same | 742, 770, 862, 895 | `resolve_worktree_conflicts` | existing `pins` (654) |
| same | 1087 | `resolve_uncommitted_conflicts` | new; on error the route's failure response |
| same | 1423, 1449, 1514, 1550 | `resolve_git_merge_conflicts` | new; on error the route's failure response |
| `apps/web-server/server/services/pr_endgame.py` | 1285, 1316 | `resolve_pr_conflicts` | existing pins (1272/1309) |
| `apps/web-server/server/routes/git.py` | 197, 986, 1191 | `initialize_git`, `download_source_update`, `squash_commits` | new; on error the route's failure response |

## Steps

1. `apps/backend/core/child_env.py:79-126`: extend the key filter in
   `merge_driver_pins` per decision 1; update the docstring.
   `tests/test_merge_driver_pins.py`: extend `test_merge_driver_pins_injected_run`
   with `filter.a.clean`, `filter.a.smudge`, `filter.a.process`,
   `filter.a.required` keys (expect the first three pinned, `required` not); add
   a runtime test that plants `filter.evil.clean="touch <marker>; cat"` and
   `* filter=evil` in `info/attributes`, modifies a tracked file, asserts plain
   `git status --porcelain` creates the marker (control, skip if not), then that
   `git *pins status --porcelain` does not.
   → `pytest tests/test_merge_driver_pins.py -q`
   Traps: keys come back as `str` decoded with surrogateescape; keep the
   `"=" in key` refusal for filter keys too.
2. Backend sites (table rows for `core/worktree.py`, `core/workspace/git_utils.py`,
   `core/workspace/merge.py`): unpack pins per decision 3. For `_run_git`
   callers put `*pins` first in the args list (`_run_git` prepends
   `git -c core.bare=false`). In `core/worktree.py` pass `env=_git_env()` to
   `merge_driver_pins` like line 1074 does. Add a test in
   `tests/test_merge_driver_pins.py` that, with the planted filter repo of step
   1, calls `WorktreeManager(repo, base_branch="main").has_uncommitted_changes()`
   and `core.workspace.git_utils.has_uncommitted_changes(repo)`: both True and
   no marker.
   → `pytest tests/test_merge_driver_pins.py tests/test_git_metadata_baseline.py -q`
   Traps: do not change other git calls; existing tests that script `_run_git`
   or `subprocess.run` may need the extra config call answered.
3. Web-server sites (table rows for `worktree_merge.py`, `pr_endgame.py`,
   `routes/git.py`): unpack pins per decision 3. `merge_driver_pins` is imported
   from `server.utils.subprocess_env` in web-server modules. For
   `run_git_command([...], path)` in `routes/git.py` put `*pins` first in the
   list (check `run_git_command` prepends `git`). `pr_endgame` pins take
   `env=`/`run=` like its existing calls.
   → `pytest tests/test_merge_driver_pins.py tests/test_pr_conflict_resolution.py tests/test_pr_endgame_conflict_loop.py apps/web-server/tests -q -k "merge or conflict or git" -p no:cacheprovider`
   Traps: `resolve_pr_conflicts` runs through an injected `runner`; tests that
   script its argv may need the pins.
4. `tests/test_merge_driver_pins.py`: AST scan over `apps/web-server/server/`,
   `apps/backend/core/worktree.py`, `apps/backend/core/workspace/`,
   `apps/backend/cli/workspace_commands.py` (skip tests). A list/tuple is a git
   argv when it has a literal `"git"` first or is the first arg of a call whose
   callee name contains `git`. Flag: subcommand `status` or `add`; `diff` whose
   other elements are all string constants starting with `-` and none of
   `--cached`/`--staged`; `worktree remove` without `--force`. A flagged argv
   passes when it has `*merge_driver_pins(...)` or `*<name>` where `<name>` is
   assigned from `merge_driver_pins` (including tuple-unpack/attribute-free
   forms used at the sites) in the same file. Include planted-source
   self-checks (unpinned flagged, pinned passes, `diff a...b` not flagged).
   → `pytest tests/test_merge_driver_pins.py -q`
   Traps: `"add"` also appears in `worktree add` and `remote add`; only treat
   `add` as the subcommand when it is the first constant after `git` (or the
   first constant for wrapper callees).
5. Late baseline:
   - `apps/backend/core/git_metadata.py:200-203`: in the late branch keep the
     `logger.warning` and add
     `print(f"WARNING: git metadata baseline taken late for {repo}; changes made before now are not detected", flush=True)  # noqa: T201`.
   - `apps/backend/cli/build_commands.py` just before the first
     `asyncio.run(run_autonomous_agent(...))` (~line 515): best-effort
     `record_baseline(working_dir)` (lazy import like `_check_qa_git_metadata`),
     `RuntimeError` → `logger.warning`.
   - `apps/backend/runners/spec_runner.py` before `orchestrator.run` (~line 312):
     same best-effort `record_baseline(project_dir)`.
   - Tests in `tests/test_git_metadata_baseline.py`: extend
     `test_missing_baseline_records_late_then_compares` to assert the WARNING
     line in `capsys` stdout; add a test that the build path records the
     baseline before `run_autonomous_agent` runs in direct mode (assert the
     baseline file exists inside a stubbed `run_autonomous_agent`), reusing the
     file's existing `handle_build_command` test scaffolding (~line 600); and one
     for spec_runner that asserts the baseline exists when the stubbed
     `SpecOrchestrator.run` is called.
   → `pytest tests/test_git_metadata_baseline.py -q`
   Traps: tests isolate `AIFACTORY_GIT_BASELINE_DIR` via conftest; the new
   import may change `scripts/gen_autonomy_matrix.py --check` output (regenerate
   if CI asks).

## Tests

`cd apps/backend && pytest ../../tests/test_merge_driver_pins.py ../../tests/test_git_metadata_baseline.py ../../tests/test_diff_no_ext_textconv.py -q`,
then `ruff check` and `ruff format --check` on the touched files. Each new test
must fail on origin/dev.

## Rollback

Revert the PR. No data or config migration; baselines already written stay valid.
