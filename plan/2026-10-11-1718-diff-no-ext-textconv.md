---
status: approved
issue: 1718
spec: spec/2026-10-11-1718-diff-no-ext-textconv.md
---

# Plan: server diffs must not run diff drivers or textconv from agent config

Worktree `/mnt/code/Source-home/GitHub/AIFactory-1718`, branch
`fix/1718-diff-no-ext-textconv`. All paths are repo-relative. Every line number
below was read from `origin/dev` 4ec9a332, which is the branch's merge base
(the branch holds only the intent/spec/plan commits on top). Line numbers stay
valid until you run `ruff format` on a file or add a line above a site, so
edit every site in a file first, then format that file once.

## Approved decisions (self-contained summary)

**Problem in one line.** An agent can write `.gitattributes` (`f diff=x`) and
`.git/config` in its worktree. A planted `diff.<x>.textconv`,
`diff.<x>.command` or `diff.external` then runs a program of the agent's
choosing on the server whenever the server runs `git diff` on that worktree
(review diff, merge security gate, insight extraction, PR context, file
evolution).

**Decisions (intent + spec approvals, carried over in full).**

- **D1. Flag every diff-family call, not only patch-printing ones** (spec
  approver decision 1, 2026-10-13). Insert `"--no-ext-diff", "--no-textconv"`
  directly after the subcommand token (`"diff"` or `"diff-tree"`) in the argv
  literal that already exists, at all 38 sites in 17 files listed in the site
  table below. Nothing else at a call changes: same `env=`, `cwd=`, `check=`,
  timeouts and output parsing. No shared `git_diff()` helper (rejected: four
  call styles, the scan test enforces the rule without a new abstraction).
- **D2. Close the `*.textconv` gap in `_is_dangerous_git_config`** in
  `apps/backend/security/git_validators.py` here, so `git config diff.x.textconv …`
  and `git -c diff.x.textconv=… …` are refused like `diff.external`,
  `diff.<x>.command` and `merge.<x>.driver` already are. `filter.*` and `lfs.*`
  stay unlisted (#1690). `diff.<x>.cachetextconv` is a boolean and stays
  allowed (the clause is `endswith(".textconv")`, which it does not match).
- **D3. Leave `git show ref:path` blob reads unchanged** (no `--no-textconv`
  on them). `git log`/`git show` are in scope only when they carry
  `-p`/`--patch`; none does today.
- **D4. Stay out of `apps/backend/core/child_env.py`, `tests/test_child_process_env.py`
  and `tests/test_github_env.py`** (#1717 / PR #1748 own them). No
  `GIT_CONFIG_*` pins, no `GIT_ATTR_SOURCE`/`core.attributesFile` change.
- **D5. Diff views lose textconv rendering; accepted.** Binary and
  LFS-pointer files render raw or as "Binary files differ"; `--numstat` shows
  `-` for them (the only `--numstat` parser, `worktree_merge.py:2288-2297`,
  already maps `-` to 0). Do not add compensating code.
- **D6. Do not add `env=` to the three backend full-patch sites** that lack
  it (`insight_extractor.py:91`, `context_gatherer.py:557`,
  `modification_tracker.py:229`). Out of scope.
- **D7. Removing a driver already planted in `.git/config`** is out of scope.
- **Tests (spec part 3).** New `tests/test_diff_no_ext_textconv.py` with an
  AST scan test (every diff-family argv under `apps/` carries both flags; the
  five full-patch files are still seen by the matcher) and a real-git
  behaviour test on `analysis.insight_extractor.get_session_diff`, parametrized
  over `diff.x.textconv`, `diff.x.command` and `diff.external`, with a control
  run that skips if the plant does not fire. Plus two payloads in
  `tests/test_command_sandbox.py:GIT_RCE_PAYLOADS`. Separate file; do not
  edit `tests/test_no_unscrubbed_spawn.py`.

**Facts checked while planning (git 2.55.0, ruff 0.14.10).**

- `git diff`/`git diff-tree` accept both flags with `--name-only`,
  `--name-status`, `--stat`, `--numstat`, `--shortstat`, `--cached --quiet`,
  `-r --no-commit-id`, and exit 0. With the flags, all three plants do not run
  and the `+` line is still printed; without them, all three fire.
- The reference test in step 1 was run against a copy of the tree: on the
  current code it fails with exactly the 38 offenders below (full-patch
  assertion passes); with the flags inserted at those 38 sites and
  `ruff format` applied, all 5 tests pass.
- All 17 call-site files and `tests/test_command_sandbox.py` are currently
  `ruff format`-clean, so `ruff format <file>` after the edit only reflows the
  lists you touched. Strict ruff counts (`standards/ruff.toml`) of the 17
  files were identical before and after insert + format.
- Existing tests that fake subprocess key on `argv[1]` (the subcommand) or on
  non-diff verbs; the subcommand stays at index 1, so none should break.
  `apps/web-server/tests/test_no_worktree_head_as_branch.py` scans argv string
  constants for ranges ending in `HEAD`; adding flags does not change that.
- Autonomy matrix: `scripts/gen_autonomy_matrix.py` is generated from
  `merge.merge_policy` / `review_tier` only. Not affected; step 9 confirms with
  `--check`.

## Site table (all 38, at 4ec9a332)

"Shape" is what the argv looks like now. For a one-line list, change
`"diff", ` to `"diff", "--no-ext-diff", "--no-textconv", `. For a multi-line
list, add the two strings as new elements on the lines right after the
subcommand. Full-patch sites are marked **FP**.

| # | File:line | Current argv (head) | Shape |
| - | --------- | ------------------- | ----- |
| 1 | apps/web-server/server/routes/worktree_merge.py:181 | `["git", "diff", "--name-status", f"{base_branch}...{work_ref}"]` | one line |
| 2 | apps/web-server/server/routes/worktree_merge.py:337 | `["git", "diff", "--name-only", f"{base_branch}...{work_ref}"]` | one line |
| 3 | apps/web-server/server/routes/worktree_merge.py:715 | `["git", "diff", "--name-only", "--diff-filter=U"]` | one line |
| 4 | apps/web-server/server/routes/worktree_merge.py:1076 | `["git", "diff", "--name-only", f"{base_branch}...{spec_branch}"]` | one line |
| 5 | apps/web-server/server/routes/worktree_merge.py:1382 | `["git", "diff", "--name-only", "--diff-filter=U"]` | one line |
| 6 | apps/web-server/server/routes/worktree_merge.py:2124 | `["git", "diff", "--stat", f"{base_branch}...{work_ref}"]` | one line |
| 7 | apps/web-server/server/routes/worktree_merge.py:2281 | `["git", "diff", "--numstat", f"{base_branch}...{work_ref}"]` | one line |
| 8 | apps/web-server/server/routes/worktree_merge.py:2323 | `["git", "diff", "--name-status", f"{base_branch}...{work_ref}"]` | one line |
| 9 | apps/web-server/server/routes/worktree_merge.py:2416 **FP** | `["git", "diff", f"{base_branch}...{work_ref}", "--", f["path"]]` | one line |
| 10 | apps/web-server/server/routes/files.py:920 | `cmd = ["git", "diff", "--name-status", base, "--"]` | one line |
| 11 | apps/web-server/server/routes/projects.py:1106 | `["git", "diff", "--shortstat", f"{base_branch}...{branch}"]` | one line |
| 12 | apps/web-server/server/services/merger.py:185 | `["git", "diff", "--name-only", f"origin/{base}...{ref}"]` (arg of `runner(...)`) | one line |
| 13 | apps/web-server/server/services/pr_endgame.py:1008 | `runner(["git", "diff", "--name-only", "--diff-filter=U"], worktree)` | one line |
| 14 | apps/backend/core/worktree.py:627 | `self._run_git(["diff", "--cached", "--name-only"])` | one line, no `"git"` |
| 15 | apps/backend/core/worktree.py:794 | `["diff", "--shortstat", f"{base}...HEAD"]` (arg of `self._run_git`) | one line, no `"git"` |
| 16 | apps/backend/core/worktree.py:1006 **FP** | `self._run_git(["diff", f"{self.base_branch}...{info.branch}"])` | one line, no `"git"` |
| 17 | apps/backend/core/worktree.py:1305 | `["diff", "--name-status", f"{self.base_branch}...{head}"]` (arg of `self._run_git`) | one line, no `"git"` |
| 18 | apps/backend/core/workspace/merge.py:656 | `["git", "diff", "--name-only", merge_base, main_commit]` | one line |
| 19 | apps/backend/core/workspace/merge.py:668 | `["git", "diff", "--name-only", merge_base, spec_commit]` | one line |
| 20 | apps/backend/core/workspace/git_utils.py:277 | `["git", "diff", "--name-status", f"{base_branch}...{spec_branch}"]` | one line |
| 21 | apps/backend/trusted_plan.py:676 | `_git(Path(project_dir), "diff", "--name-only", resolved, "HEAD")` | varargs: becomes `_git(Path(project_dir), "diff", "--no-ext-diff", "--no-textconv", "--name-only", resolved, "HEAD")` |
| 22 | apps/backend/security/git_validators.py:226 (227 after step 2's clause) | `["git", "diff", "--cached", "--name-only"]` | one line |
| 23 | apps/backend/security/scan_secrets.py:387 | `["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"]` | one line |
| 24 | apps/backend/agents/utils.py:110 | `["git", "diff", "--cached", "--quiet"]` | one line |
| 25 | apps/backend/analysis/insight_extractor.py:91 **FP** | `["git", "diff", commit_before, commit_after]` | one line |
| 26 | apps/backend/analysis/insight_extractor.py:136 | `["git", "diff", "--name-only", commit_before, commit_after]` | one line |
| 27 | apps/backend/runners/github/context_gatherer.py:557-562 **FP** | `asyncio.create_subprocess_exec(` / `"git",` (558) / `"diff",` (559) / `f"{base_ref}...{head_ref}",` / `"--",` / `path,` | multi-line positional args: add `"--no-ext-diff",` and `"--no-textconv",` as two lines after 559 |
| 28 | apps/backend/merge/timeline_git.py:108-116 | `[` / `"git",` (109) / `"diff-tree",` (110) / `"--no-commit-id",` / `"--name-only",` / `"-r",` / `commit_hash,` | multi-line: add the two lines after 110 |
| 29 | apps/backend/merge/timeline_git.py:159 | `["git", "diff-tree", "--stat", "--no-commit-id", commit_hash]` | one line (`"diff-tree", ` → `"diff-tree", "--no-ext-diff", "--no-textconv", `) |
| 30 | apps/backend/merge/timeline_git.py:241 | `["git", "diff", "--name-only", f"{target_branch}...{head}"]` | one line |
| 31 | apps/backend/merge/file_evolution/modification_tracker.py:204 | `["git", "diff", "--name-only", f"{target_branch}...{head}"]` | one line |
| 32 | apps/backend/merge/file_evolution/modification_tracker.py:229 **FP** | `["git", "diff", f"{target_branch}...{head}", "--", file_path]` | one line |
| 33 | apps/backend/cli/workspace_commands.py:159 | `["git", "diff", "--name-only", f"{base_branch}...{head}"]` | one line |
| 34 | apps/backend/cli/workspace_commands.py:178 | `["git", "diff", "--name-only", base_branch, head]` | one line |
| 35 | apps/backend/cli/workspace_commands.py:287 | `["git", "diff", "--staged", "--stat"]` | one line |
| 36 | apps/backend/cli/workspace_commands.py:297 | `["git", "diff", "--staged", "--name-only"]` | one line |
| 37 | apps/backend/cli/workspace_commands.py:532 | `["git", "diff", "--name-only", merge_base, result["base_branch"]]` | one line |
| 38 | apps/backend/cli/workspace_commands.py:545 | `["git", "diff", "--name-only", merge_base, spec_branch]` | one line |

Not sites (leave alone): dict keys `"diff": …` (`worktree_merge.py:2408,2423,2426`,
`insight_extractor.py:225,308`, `runners/github/sanitize.py`, `testing.py`,
`gitlab_provider.py`, `task_control.py`); the `gh` CLI argv
`["pr", "diff", …]` in `runners/github/gh_client.py:74,383`; the command-name
list `project/command_registry/base.py:49`; `git show ref:path` at
`worktree_merge.py:1148,1174,1198`, `core/workspace/git_utils.py:248`,
`runners/github/context_gatherer.py:507-509` (D3); `git log --name-status`
at `core/workspace/git_utils.py:121-128` (no `-p`).

## Repo traps (apply to every step that edits Python)

- **T1 venv.** The worktree has no venv. Use
  `V=/mnt/code/Source-home/GitHub/AIFactory/apps/backend/.venv/bin` and run
  everything from the worktree root:
  `cd /mnt/code/Source-home/GitHub/AIFactory-1718`.
- **T2 ratchets run on staged files.** After `git add`, before each commit:
  `$V/python scripts/cq_ratchet.py --tool ruff --staged --ruff $V/ruff --config standards/ruff.toml --paths 'apps/backend/*.py' 'apps/web-server/*.py' 'scripts/*.py'`
  and
  `$V/python scripts/cq_ratchet.py --tool mypy --staged --mypy $V/mypy --config standards/mypy.ini --paths 'apps/backend/*.py' 'apps/web-server/*.py' 'scripts/*.py'`.
  Both must exit 0. Only argv literals change, so counts must not move; if
  one does, you edited more than the argv.
- **T3 format.** `$V/ruff format --config ruff.toml <files you touched>` once
  per file, after all its sites are edited. Inserting the flags pushes most
  one-line lists past 88 columns; ruff reflows them to one element per line.
  That is expected. Keep any `# noqa: S607`/`S603` on the call it belonged to.
  Then `$V/ruff format --check --config ruff.toml <files>` must pass.
- **T4 vendored standard.** `standards/coding-standards.md` (and the rest of
  `standards/`) is vendored. Do not edit it.
- **T5 scope.** Do not edit `apps/backend/core/child_env.py`,
  `tests/test_child_process_env.py`, `tests/test_github_env.py`,
  `tests/test_no_unscrubbed_spawn.py`, or the intent/spec files. Do not add
  `env=`, a helper, or flags on `git show ref:path`.
- **T6 commit messages** cite the plan step, e.g.
  `fix(security): pass --no-ext-diff --no-textconv on web-server diffs (#1718) [plan step 3]`,
  and end with the session's attribution lines. A deviation from this plan
  updates this file in the same commit as the code.

## Steps

0. **Preflight, no edit.** `git status` clean on
   `fix/1718-diff-no-ext-textconv`; `git fetch origin && git merge-base HEAD origin/dev`
   is 4ec9a332 (if `origin/dev` moved, rebase first and re-grep; the scan
   test in step 1 is the source of truth for the site list). Re-grep:
   `grep -rnE '"diff(-tree|-index|-files)?",' apps --include=*.py | grep -v /tests/ | grep -v command_registry`
   → verify the 38 rows of the site table (context_gatherer and timeline_git:110
   appear by their subcommand line).
   Traps: T1.

1. **`tests/test_diff_no_ext_textconv.py` (new file): add the scan and
   behaviour tests, red first.** Create the file with exactly the content in
   "Reference test file" below.
   → verify by `$V/pytest tests/test_diff_no_ext_textconv.py -q`:
   `test_every_git_diff_call_disables_drivers_and_textconv` FAILS and its
   message lists exactly the 38 sites (timeline_git shows `:108` and
   context_gatherer `:557`, the node's first line);
   `test_scan_still_sees_the_full_patch_sites` PASSES; the three
   `test_session_diff_does_not_run_a_planted_program[...]` FAIL with
   "get_session_diff ran the planted …" (if they SKIP, the runner's git
   ignores the plant; stop and report). `$V/ruff format --check --config ruff.toml`
   and `$V/ruff check --config standards/ruff.toml` on the file are clean.
   Commit the red test alone (tests are outside the ratchet paths, but run T2
   anyway; it reports no changed files).
   Traps: T1, T3, T6. The test sits in `tests/`, which `tests/conftest.py:92`
   puts `apps/backend` on `sys.path` for, so `from analysis.insight_extractor import …` works.

2. **`apps/backend/security/git_validators.py` + `tests/test_command_sandbox.py`:
   textconv in the validator, and site #22.**
   - Docstring at `git_validators.py:32-36`: change
     "external diff and diff drivers," to
     "external diff, diff drivers and textconv (#1718),". Re-wrap the
     docstring lines to stay within 88 columns; do not change the rest.
   - In the `return (...)` chain, directly after line 55
     `        or k.endswith(".driver")`, add one line:
     `        or k.endswith(".textconv")  # diff.<x>.textconv runs a program (#1718)`.
   - Site #22, now at line 227 (226 before the clause):
     `["git", "diff", "--cached", "--name-only"]` →
     `["git", "diff", "--no-ext-diff", "--no-textconv", "--cached", "--name-only"]`.
   - `tests/test_command_sandbox.py`: in `GIT_RCE_PAYLOADS` (72-102), add two
     entries before the closing `]` on line 102, after
     `"git config protocol.ext.allow always",`:
     `"git config diff.x.textconv cat",` and
     `"git -c diff.x.textconv=cat diff",`.
   → verify by `$V/pytest tests/test_command_sandbox.py tests/test_security.py -q`
   (all pass; the two new parametrized cases of
   `TestGitOptionHardening.test_git_rce_options_blocked` pass, and
   `"git diff HEAD"` in `LEGIT_COMMANDS` still passes). Then
   `$V/pytest tests/test_diff_no_ext_textconv.py -q` lists 37 offenders.
   Traps: T1, T2, T3, T5 (`diff.<x>.cachetextconv` must stay allowed: do not
   use `"textconv" in k`), T6.

3. **Web-server, 13 sites (#1-#13).** Files:
   `apps/web-server/server/routes/worktree_merge.py` (181, 337, 715, 1076,
   1382, 2124, 2281, 2323, 2416), `apps/web-server/server/routes/files.py:920`,
   `apps/web-server/server/routes/projects.py:1106`,
   `apps/web-server/server/services/merger.py:185`,
   `apps/web-server/server/services/pr_endgame.py:1008`. Change per the site
   table. Do not touch the `--numstat` parser at `worktree_merge.py:2288-2297`
   (it already maps `-` to 0, D5).
   → verify by `$V/pytest tests/test_diff_no_ext_textconv.py -q` (24
   offenders left, none under `apps/web-server`), then
   `$V/pytest tests/test_discard_resolves_branch.py tests/audit/test_task_action_audit.py tests/test_completion_lands_via_merger.py tests/test_pr_endgame_conflict_loop.py tests/test_pr_conflict_resolution.py tests/test_file_containment.py tests/test_no_unscrubbed_spawn.py -q`
   and `cd apps/web-server && $V/pytest tests/test_no_worktree_head_as_branch.py tests/test_control_plane_reads_the_pushed_work.py tests/test_semantic_conflicts_read_the_pushed_work.py -q`
   (all pass).
   Traps: T1, T2, T3, T5, T6. If a test fakes subprocess by comparing a full
   diff argv, update that test's expected argv (not the code) and note it as
   a deviation in this plan.

4. **Backend core, 8 sites (#14-#21).** Files:
   `apps/backend/core/worktree.py` (627, 794, 1006, 1305; these use
   `self._run_git([...])`, so the list starts with `"diff"`, no `"git"`),
   `apps/backend/core/workspace/merge.py` (656, 668),
   `apps/backend/core/workspace/git_utils.py:277`,
   `apps/backend/trusted_plan.py:676` (varargs form, see table).
   → verify by `$V/pytest tests/test_diff_no_ext_textconv.py -q` (16 left),
   then `$V/pytest tests/test_worktree_security_gate.py tests/test_worktree_commit_logging.py tests/test_worktree_merge_dirty.py tests/test_worktree_gitignore.py tests/test_worktree_fresh_base.py tests/test_sync_merge.py tests/test_tfactory_auto_handoff.py tests/test_contract_trust.py tests/test_from_plan_gate_ordering.py tests/test_review_tier.py -q`
   (all pass).
   Traps: T1, T2, T3, T5 (PR #1748 may also touch `core/worktree.py`; edit
   argv literals only so a rebase conflict stays small), T6.

5. **Backend security/agents/analysis/runners, 5 sites (#23-#27).** Files:
   `apps/backend/security/scan_secrets.py:387`,
   `apps/backend/agents/utils.py:110`,
   `apps/backend/analysis/insight_extractor.py` (91, 136),
   `apps/backend/runners/github/context_gatherer.py:557-562` (multi-line
   positional args: insert `"--no-ext-diff",` and `"--no-textconv",` as two
   new lines after `"diff",` on 559). No `env=` added (D6).
   → verify by `$V/pytest tests/test_diff_no_ext_textconv.py -q`: 11 left,
   and the three behaviour cases now PASS. Then
   `$V/pytest tests/test_scan_secrets.py tests/test_gitleaks_config.py tests/test_security_scanner.py tests/test_insight_extractor_batch.py tests/test_parse_insights.py -q`
   (all pass).
   Traps: T1, T2, T3, T5, T6.

6. **Backend merge, 5 sites (#28-#32).** Files:
   `apps/backend/merge/timeline_git.py` (108-116 multi-line: two new lines
   after `"diff-tree",` on 110; 159 `"diff-tree", "--stat"…` one line; 241),
   `apps/backend/merge/file_evolution/modification_tracker.py` (204, 229).
   → verify by `$V/pytest tests/test_diff_no_ext_textconv.py -q` (6 left,
   all in `cli/workspace_commands.py`) and
   `$V/pytest tests -q -k "timeline or evolution or modification"` (all pass
   or none selected).
   Traps: T1, T2, T3, T6.

7. **CLI, 6 sites (#33-#38).** File `apps/backend/cli/workspace_commands.py`
   (159, 178, 287, 297, 532, 545).
   → verify by `$V/pytest tests/test_diff_no_ext_textconv.py -q` (5 passed,
   0 offenders) and `$V/pytest tests/test_gen_autonomy_matrix.py tests/test_no_unscrubbed_spawn.py -q`
   (all pass).
   Traps: T1, T2, T3, T6.

8. **`CHANGELOG.md`: entry under `## [Unreleased]` → `### Security`**, as the
   first bullet (before the #1717 bullet at line 5):

   ```markdown
   - **Server diffs no longer run a diff driver or textconv from the agent's repo config (#1718).**
     Every server-side `git diff`/`git diff-tree` now passes `--no-ext-diff --no-textconv`,
     so a `diff.<x>.textconv`, `diff.<x>.command` or `diff.external` planted in a
     worktree's `.git/config` cannot run a program when the server reviews, gates or
     summarises that worktree. The command validator also refuses
     `git config diff.<x>.textconv` and `git -c diff.<x>.textconv=…`. Binary and
     LFS-pointer files now show raw (or as "Binary files differ") in diff views and
     the merge security gate, and count as 0 lines in `--numstat`.
   ```
   → verify by `sed -n 1,16p CHANGELOG.md` (entry is first under Security).
   Traps: T4, T6. One commit for the CHANGELOG, or fold it into step 7's.

9. **Final verification, no edit unless something fails.** Run the Tests
   section. Then `$V/python scripts/gen_autonomy_matrix.py --check` → exit 0
   (the matrix is generated from `merge_policy`, which this change does not
   touch; if it does report stale, run it without `--check`, commit
   `docs/docs/compliance/autonomy-matrix.md` and
   `docs/static/compliance/autonomy-matrix.json`, and record the deviation
   here). `git diff --stat origin/dev...HEAD -- apps tests CHANGELOG.md` shows
   exactly the 20 files under "Files touched". `git push`.
   Traps: T1, T2, T5.

## Files touched

17 call-site files (site table), `tests/test_command_sandbox.py`, new
`tests/test_diff_no_ext_textconv.py`, `CHANGELOG.md`: 20 files (22 with the two fake-runner tests in Deviations).
`apps/backend/security/git_validators.py` is one of the 17 (step 2 does both
its clause and its site).

## Reference test file

Content for `tests/test_diff_no_ext_textconv.py` (step 1). Already
`ruff format`-clean and `standards/ruff.toml`-clean; checked red on the
current tree (38 offenders, 3 behaviour failures) and green with the flags.

```python
"""Server diffs must not run a diff driver or textconv from agent config (#1718).

An agent can write ``.gitattributes`` and ``.git/config`` in its worktree. A
``diff.<x>.textconv``, ``diff.<x>.command`` or ``diff.external`` planted there
runs a program when the server diffs that worktree. Every diff-family git call
under ``apps/`` must therefore pass ``--no-ext-diff --no-textconv``.
"""

from __future__ import annotations

import ast
import itertools
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_APPS = _ROOT / "apps"

_DIFF_CMDS = {"diff", "diff-tree", "diff-index", "diff-files"}
_PATCH_CMDS = {"log", "show"}  # only when they also carry -p / --patch
_PATCH_FLAGS = {"-p", "--patch"}
_REQUIRED = {"--no-ext-diff", "--no-textconv"}
_SUMMARY = {
    "--name-only",
    "--name-status",
    "--stat",
    "--numstat",
    "--shortstat",
    "--quiet",
}
_SKIP_DIRS = {"tests", "test", "node_modules", "__pycache__", "site-packages", "venv"}

# The full-patch sites the issue names. The matcher must keep seeing them.
_FULL_PATCH_FILES = {
    "apps/web-server/server/routes/worktree_merge.py",
    "apps/backend/core/worktree.py",
    "apps/backend/analysis/insight_extractor.py",
    "apps/backend/runners/github/context_gatherer.py",
    "apps/backend/merge/file_evolution/modification_tracker.py",
}


def _sources() -> Iterator[tuple[Path, str]]:
    for path in sorted(_APPS.rglob("*.py")):
        rel = path.relative_to(_ROOT)
        if any(p in _SKIP_DIRS or p.startswith(".") for p in rel.parts[:-1]):
            continue
        if path.name.startswith("test_") or path.name == "conftest.py":
            continue
        yield path, rel.as_posix()


def _callee(call: ast.Call) -> str:
    f = call.func
    if isinstance(f, ast.Name):
        return f.id.lower()
    if isinstance(f, ast.Attribute):
        return f.attr.lower()
    return ""


def _strs(elts: list[ast.expr]) -> list[str | None]:
    return [
        e.value if isinstance(e, ast.Constant) and isinstance(e.value, str) else None
        for e in elts
    ]


def _is_diff_family(sub: str, consts: set[str]) -> bool:
    return sub in _DIFF_CMDS or (sub in _PATCH_CMDS and bool(consts & _PATCH_FLAGS))


def _git_diff_calls() -> Iterator[tuple[str, int, set[str]]]:
    """Yield (path, line, string constants) for every git diff-family argv."""
    for path, rel in _sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        arg_of: dict[int, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for a in node.args:
                    arg_of[id(a)] = _callee(node)
        for node in ast.walk(tree):
            if isinstance(node, (ast.List, ast.Tuple)):
                elts, callee = node.elts, arg_of.get(id(node), "")
            elif isinstance(node, ast.Call):
                elts, callee = node.args, _callee(node)
            else:
                continue
            vals = _strs(elts)
            consts = {v for v in vals if v is not None}
            sub = next(
                (
                    b
                    for a, b in itertools.pairwise(vals)
                    if a == "git" and b is not None
                ),
                None,
            )
            if sub is None and "git" in callee:
                sub = next((v for v in vals if v is not None), None)
            if sub is not None and _is_diff_family(sub, consts):
                yield rel, node.lineno, consts


def test_every_git_diff_call_disables_drivers_and_textconv() -> None:
    calls = list(_git_diff_calls())
    offenders = [f"{p}:{n}" for p, n, c in calls if not _REQUIRED.issubset(c)]
    assert not offenders, (
        "git diff-family calls missing --no-ext-diff --no-textconv (#1718):\n"
        + "\n".join(offenders)
    )


def test_scan_still_sees_the_full_patch_sites() -> None:
    full_patch = {p for p, _, c in _git_diff_calls() if not c & _SUMMARY}
    missing = _FULL_PATCH_FILES - full_patch
    assert not missing, f"matcher no longer finds the full-patch diff in: {missing}"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.mark.parametrize("key", ["diff.x.textconv", "diff.x.command", "diff.external"])
def test_session_diff_does_not_run_a_planted_program(tmp_path: Path, key: str) -> None:
    from analysis.insight_extractor import get_session_diff  # noqa: PLC0415

    repo = tmp_path / "repo"
    repo.mkdir()
    marker = tmp_path / "ran"
    plant = tmp_path / "plant.sh"
    plant.write_text(f'#!/bin/sh\ntouch "{marker}"\nexit 0\n')
    plant.chmod(0o755)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / ".gitattributes").write_text("f diff=x\n")
    (repo / "f").write_text("a\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "one")
    c1 = _git(repo, "rev-parse", "HEAD")
    (repo / "f").write_text("a\nadded-line\n")
    _git(repo, "commit", "-qam", "two")
    c2 = _git(repo, "rev-parse", "HEAD")
    _git(repo, "config", key, str(plant))

    # Control: plain git diff must fire the plant, or the check below is vacuous.
    subprocess.run(  # noqa: S603
        ["git", "diff", c1, c2],  # noqa: S607
        cwd=repo,
        capture_output=True,
        check=False,
    )
    if not marker.exists():
        pytest.skip(f"this git ignores a planted {key}")
    marker.unlink()

    out = get_session_diff(repo, c1, c2)
    assert not marker.exists(), f"get_session_diff ran the planted {key}"
    assert "+added-line" in out
```

## Tests

From `/mnt/code/Source-home/GitHub/AIFactory-1718`, with
`V=/mnt/code/Source-home/GitHub/AIFactory/apps/backend/.venv/bin`:

| Command | Expected |
| ------- | -------- |
| `$V/pytest tests/test_diff_no_ext_textconv.py -q` | 5 passed: scan finds 0 offenders, the five full-patch files are seen, the three plants (`diff.x.textconv`, `diff.x.command`, `diff.external`) do not run and `+added-line` is in the output. Not skipped. |
| `$V/pytest tests/test_command_sandbox.py tests/test_security.py -q` | all pass, including the two new textconv payloads |
| `$V/pytest tests/test_no_unscrubbed_spawn.py -q` | pass (unchanged) |
| `$V/pytest tests -q` | no new failures versus `origin/dev` (compare with `git stash`-free run on a dev checkout if anything fails) |
| `cd apps/web-server && $V/pytest tests -q` | no new failures versus `origin/dev` |
| `$V/ruff check --config ruff.toml` and `$V/ruff format --check --config ruff.toml` on the 20 files' `.py` members | clean |
| T2 ratchet commands (ruff and mypy, `--staged`) before each commit | exit 0 |
| `$V/python scripts/gen_autonomy_matrix.py --check` | exit 0 |
| Manual (after deploy to a dev server): in a task worktree, `echo 'f diff=x' > .gitattributes` and `git config diff.x.textconv 'sh -c "touch /tmp/ran-1718; cat"'`, change `f`, open the task diff in the UI | diff renders, `/tmp/ran-1718` does not exist on the web-server pod |

Red-first proof: after step 1 alone, the scan test fails listing all 38
sites; that shows the matcher works.

## Rollback

Code-only change; no data, schema, config or migration. To undo:

- Before merge: `git revert <commits of steps 1-8>` on the branch, or close
  the PR.
- After merge: `git revert -m 1 <merge commit>` (or revert the squash
  commit) on `dev`, and redeploy the web-server and backend images. Reverting
  restores textconv rendering and re-opens the hole; the plans and specs stay.
- Partial rollback is possible per site (remove the two tokens), but the scan
  test then fails by design; delete or `xfail` it in the same revert.

## Deviations during implementation

- Step 3: `tests/test_pr_endgame_conflict_loop.py` and
  `tests/test_pr_conflict_resolution.py` fake the runner keyed on the exact
  string `"diff --name-only --diff-filter=U"`; their keys became
  `"diff --no-ext-diff --no-textconv --name-only --diff-filter=U"` (7 keys).
  Two more files touched, so 22 files in total.

## Approver decisions (2026-10-13)

None beyond plan approval.
- Review hardening (after the Opus review): the scan test now requires both
  flags to come before any `"--"` in the argv (git reads anything after `--`
  as a pathspec), with a small self-test, and its docstring states the
  literal-argv limit. `git show <commit>` without `-p`, `merge-tree` merge
  drivers and other non-diff driver paths are filed as a follow-up issue.
- `apps/web-server/tests/test_merger.py` fakes the runner on the exact
  `"diff --name-only"` key too; its 8 keys now carry the two flags (found by
  the full web-server suite after step 3).
