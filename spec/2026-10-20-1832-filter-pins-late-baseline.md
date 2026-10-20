---
status: approved
issue: 1832
intent: intent/2026-10-20-1832-filter-pins-late-baseline.md
---

# Spec: pin filters on server status/add/diff, and make a late baseline visible (#1832 items 1 and 3)

## Design

### A. Filter pins in the existing helper (item 3)

`merge_driver_pins` (`apps/backend/core/child_env.py:79`) already lists every
config key name (`git config -z --includes --name-only --list`) and returns
`-c merge.<x>.driver=` for each driver. It also returns `-c <key>=` for every
key matching `filter.<x>.clean`, `filter.<x>.smudge` and `filter.<x>.process`.
Same parsing, same `=` refusal, same baseline check first. Name and signature
unchanged; the docstring says it now covers filters.

Checked on git 2.54: with the three keys pinned empty, `status`, `add` and
`diff` run no filter. With `filter.<x>.required=true` they exit 128 instead,
which fails closed. Because merges, rebases and previews already unpack these
pins, their checkouts stop running smudge filters too.

The pins go on every server-side `git status`, `git add` and `git diff` that
reads the worktree (no revisions and no `--cached`/`--staged`). The sites:

| File | Lines (origin/dev) | Function |
| --- | --- | --- |
| `apps/backend/core/worktree.py` | 1107 | `_do_merge_locked` (reuses its `pins`) |
| `apps/backend/core/worktree.py` | 1205, 1408 | `commit_in_worktree`, `has_uncommitted_changes` |
| `apps/backend/core/workspace/git_utils.py` | 200 | `has_uncommitted_changes` |
| `apps/backend/core/workspace/merge.py` | 826, 947, 957, 1008, 1128, 1162, 1176 | `_resolve_git_conflicts_with_ai` |
| `apps/web-server/server/routes/worktree_merge.py` | 327 | `get_worktree_merge_preview` |
| same | 742, 770, 862, 895 | `resolve_worktree_conflicts` (reuses its `pins`) |
| same | 1087 | `resolve_uncommitted_conflicts` |
| same | 1423, 1449, 1514, 1550 | `resolve_git_merge_conflicts` |
| `apps/web-server/server/services/pr_endgame.py` | 1285, 1316 | `resolve_pr_conflicts` (reuses its pins) |
| `apps/web-server/server/routes/git.py` | 197, 986, 1191 | `initialize_git`, `download_source_update`, `squash_commits` |

Where a function already holds pins for the same repo, it reuses them.
Otherwise it calls `merge_driver_pins(<cwd>)` once, and a `RuntimeError` takes
that site's existing git-failure path (for a status probe: report "has
changes"/refuse, never "clean").

`git worktree remove`: every server call passes `--force`
(`core/worktree.py:843`, `:943`, `worktree_merge.py:1985`, `:2603`), and with
`--force` git does not run `status` in the worktree, so no filter runs (checked
on git 2.54). No change there; the scan below keeps requiring `--force` or pins.

A regression scan (AST, like `tests/test_diff_no_ext_textconv.py`) walks the
server-side files (`apps/web-server/server/`, `apps/backend/core/worktree.py`,
`apps/backend/core/workspace/`, `apps/backend/cli/workspace_commands.py`) and
fails on any literal `status`/`add`/worktree-reading `diff` argv without
unpacked pins, and on `worktree remove` without `--force` or pins.

### B. Earlier baseline and a visible late warning (item 1)

- `runners/spec_runner.py`: before `orchestrator.run(...)`, call
  `record_baseline(project_dir)` (never overwrites). This covers spec-pipeline
  agents that run before `create_worktree`.
- `cli/build_commands.py`: before the first `run_autonomous_agent`, call
  `record_baseline(working_dir)`. This covers direct/CLI-only mode and builds
  where workspaces are disabled. Isolated mode already recorded it in
  `create_worktree`; the call is then a no-op.
- Both are best-effort: on `RuntimeError` (not a git repo, unwritable dir) log
  a warning and go on; the check later falls back to the late baseline.
- `check_git_metadata` keeps taking the late baseline and passing, but now
  also prints `WARNING: git metadata baseline taken late for <repo>; changes
  made before now are not detected` to stdout (the build log the user reads)
  besides the existing `logger.warning`.

## Alternatives rejected

- A new `filter_pins` helper: the task says reuse; it would duplicate the
  config listing and the baseline check.
- Pin only local/worktree-scope filters: needs `--show-scope` parsing; the
  image already runs `git lfs uninstall --system` (`Dockerfile:195`), so no
  legitimate global filter is lost.
- Pin `filter.<x>.required=false` too: makes `git add` store unfiltered content
  silently; failing (rc 128) is safer.
- Fail closed on a missing baseline: breaks direct/CLI-only mode. Left as the
  open question below.
- Pinning `stash`/`checkout` sites: they also run filters but are outside
  item 3's list; noted as a follow-up.

## Risks

- `merge_driver_pins` now runs at status/add sites, so a git-metadata drift
  refuses those too (fail closed). A non-git directory raises there; each site
  maps it to its failure path.
- A repo that legitimately uses a local `filter.*` (for example `git lfs install
  --local`) gets pointer files / raw content on server add and status, or rc 128
  when `required=true`.
- A resumed direct-mode build whose earlier sessions ran before this change gets
  its baseline at build start without the late warning (one-time migration, as
  in #1818).
- Late-baseline print goes to the web-server pod log for server-side checks,
  not to the UI.

## Open question for the user

Should a missing baseline fail closed (refuse) instead of warning? It would
break direct/CLI-only mode unless that mode records its own baseline first.
Not changed here.

## Verification

- `tests/test_merge_driver_pins.py`: injected-run unit test expects filter
  pins; a runtime test plants `filter.evil.clean` + `* filter=evil` and asserts
  `WorktreeManager.has_uncommitted_changes` and
  `core.workspace.git_utils.has_uncommitted_changes` do not run it (fails on
  dev); the AST scan above (fails on dev).
- `tests/test_git_metadata_baseline.py`: late check prints the warning to
  stdout (fails on dev); `build_commands` records the baseline before the agent
  runs in direct mode; `spec_runner` records it before the orchestrator runs.
- Existing `tests/test_merge_driver_pins.py`, `tests/test_git_metadata_baseline.py`,
  `tests/test_diff_no_ext_textconv.py`, `ruff check`, `ruff format --check`.
