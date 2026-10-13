---
status: draft
issue: 1718
intent: intent/2026-10-11-1718-diff-no-ext-textconv.md
---

# Spec: server diffs must not run diff drivers or textconv from agent config

## Design

Three parts. Every one follows an approver decision recorded in the intent.

### 1. Pass `--no-ext-diff --no-textconv` on every diff-family git call

Insert both flags directly after the subcommand, in the argv literal that
already exists at each site. Nothing else at the call changes: same `env=`,
`cwd=`, `check=`, timeouts and output parsing.

```python
["git", "diff", "--no-ext-diff", "--no-textconv", f"{base_branch}...{work_ref}", "--", f["path"]]
self._run_git(["diff", "--no-ext-diff", "--no-textconv", f"{self.base_branch}...{info.branch}"])
```

The five full-patch sites named in the intent are the ones that matter for
security:

- `apps/web-server/server/routes/worktree_merge.py:2416` (per-file review diff)
- `apps/backend/core/worktree.py:1006` (`merge_worktree` → `security_pre_merge_gate_sync` input)
- `apps/backend/analysis/insight_extractor.py:91` (`get_session_diff`)
- `apps/backend/runners/github/context_gatherer.py:559` (`asyncio.create_subprocess_exec("git", "diff", ...)`)
- `apps/backend/merge/file_evolution/modification_tracker.py:229`

The approver also asked for a scan test that requires the flags on every
diff-family call (part 3). For that test to pass, the stat, numstat,
name-only, name-status, `--quiet` and `diff-tree` calls get the same two
flags. On dev that is these sites (grep at the branch base):

- `apps/web-server/server/routes/worktree_merge.py`: 181, 337, 715, 1076, 1382, 2124, 2281, 2323
- `apps/web-server/server/routes/files.py:920`
- `apps/web-server/server/routes/projects.py:1106`
- `apps/web-server/server/services/merger.py:185`
- `apps/web-server/server/services/pr_endgame.py:1008`
- `apps/backend/core/worktree.py`: 627, 794, 1305
- `apps/backend/core/workspace/merge.py`: 656, 668
- `apps/backend/core/workspace/git_utils.py:277`
- `apps/backend/trusted_plan.py:676` (`_git(repo, "diff", ...)` varargs form)
- `apps/backend/security/git_validators.py:226`
- `apps/backend/security/scan_secrets.py:387`
- `apps/backend/agents/utils.py:110`
- `apps/backend/analysis/insight_extractor.py:136`
- `apps/backend/merge/timeline_git.py`: 110 (`diff-tree`), 159 (`diff-tree --stat`), 241
- `apps/backend/merge/file_evolution/modification_tracker.py:204`
- `apps/backend/cli/workspace_commands.py`: 159, 178, 287, 297, 532, 545

38 sites in 17 files, one token pair each. The plan re-greps at
implementation time; the scan test is the source of truth for the final list.

Checked with git 2.55.0: `diff` and `diff-tree` accept both flags together
with `--name-only`, `--name-status`, `--stat`, `--numstat`, `--shortstat`,
`--cached --quiet` and `-r --no-commit-id`, and exit 0. With the flags, a
planted `diff.x.textconv` does not run.

`git show ref:path` blob reads stay unchanged (decision 3). No server code
calls `git log -p` or `git show <commit>` today.

### 2. Close the `*.textconv` gap in `_is_dangerous_git_config`

In `apps/backend/security/git_validators.py:42-58`, add one clause next to
`k.endswith(".driver")`:

```python
or k.endswith(".textconv")  # diff.<x>.textconv runs a program (#1718)
```

and name textconv in the docstring. This makes `git config diff.x.textconv …`
and `git -c diff.x.textconv=… …` fail validation through the same path that
already blocks `diff.external`, `diff.<x>.command` and `merge.<x>.driver`.
`filter.*` and `lfs.*` stay unlisted (#1690). `diff.<x>.cachetextconv` is a
boolean and stays allowed.

### 3. Tests

New file `tests/test_diff_no_ext_textconv.py`, in two parts.

**Scan test** (AST, same style as `tests/test_no_unscrubbed_spawn.py`, but a
separate file so that test is left alone). It walks every `*.py` under `apps/`,
skipping test directories. In each `ast.List`, `ast.Tuple` and each call's
positional args it reads the string constants in order. The sequence is a git
diff-family call when:

- a constant `"git"` comes directly before a `diff`, `diff-tree`,
  `diff-index` or `diff-files` token (direct `subprocess` / `asyncio` /
  `runner([...])` calls), or the token is the first string constant and the
  sequence is an argument to a callee whose name contains `git` (`_run_git`,
  `_git`, `run_git`); or
- the subcommand is `log` or `show` and the sequence also carries `-p` or
  `--patch` (none today; it covers the `log -p` that #1718 names).

Every such sequence must contain both `"--no-ext-diff"` and `"--no-textconv"`.
The failure message lists `path:line` for each offender. `["pr", "diff", …]`
(the `gh` CLI in `runners/github/gh_client.py`) does not match, because no
`"git"` precedes it and its callee is `client.run`. Dict keys such as
`{"diff": ...}` are not sequences.

The test also asserts that the five full-patch sites above are among the calls
it found, so a change to the matcher cannot silently drop them.

**Behaviour test** (real git, `tmp_path`, no network). It builds a two-commit
repo, writes `f diff=x` to `.gitattributes`, and plants a program that touches
a marker file. It is parametrized over the three ways to plant one:
`diff.x.textconv`, `diff.x.command`, and `diff.external`. It then:

- calls the real `analysis.insight_extractor.get_session_diff(repo, c1, c2)`
  and asserts that the marker is absent and the result contains the real `+`
  line;
- control: runs plain `git diff c1 c2` in the same repo and asserts that the
  marker appears. This proves the plant works, so the absent marker means
  something. If the git on the runner ignores the plant, the test skips
  rather than passing vacuously.

`get_session_diff` is the one full-patch site callable without a server or a
`WorktreeManager` fixture. The scan test proves the other four carry the same
argv.

**Validator test**: add `"git config diff.x.textconv cat"` and
`"git -c diff.x.textconv=cat diff"` to `GIT_RCE_PAYLOADS` in
`tests/test_command_sandbox.py`. The existing
`TestGitOptionHardening.test_git_rce_options_blocked` then covers them, and
`"git diff HEAD"` in `LEGIT_COMMANDS` stays allowed.

### Files touched

The 17 call-site files listed in part 1, `apps/backend/security/git_validators.py`,
`tests/test_command_sandbox.py`, and the new `tests/test_diff_no_ext_textconv.py`.
The spec does not touch `apps/backend/core/child_env.py`,
`tests/test_child_process_env.py` or `tests/test_github_env.py` (#1717 /
PR #1748).

## Alternatives rejected

- **Pin `diff.external=` and similar keys in `child_env()` (`GIT_CONFIG_*`).**
  No git config key turns textconv or `diff.<x>.command` off: they are keyed
  by the attribute name the agent picks. The intent also rules out
  `child_env.py` while PR #1748 is open.
- **`GIT_ATTR_SOURCE` / `--attr-source` set to an empty tree, or
  `core.attributesFile=/dev/null`.** The repo's own `.gitattributes` is still
  read in the worktree case. An empty attr source also changes `eol`/`text`
  and `merge=` handling for every git call, not only diffs. That is a wider
  behaviour change than the problem needs.
- **A shared `git_diff()` helper that adds the flags.** It would mean changing
  four different call styles (`subprocess.run`, `_run_git`, `_git` varargs,
  `asyncio.create_subprocess_exec`, injected `runner`). The scan test enforces
  the same rule with no new abstraction. Inline flags are the smallest change.
- **Flags only on the five full-patch sites, with the scan limited to
  patch-printing calls.** This is smaller (5 edits instead of 38), and
  the stat/name-only calls did not run either program in the intent's test.
  The approver chose every diff-family call, so the scan test cannot tell a
  safe stat call from a future patch call that someone adds by editing flags.
  This choice is recorded as an open question below, in case "every
  diff-family call" meant only patch-printing ones.
- **Removing or rewriting a driver already planted in `.git/config`.** The
  intent puts this out of scope.
- **Adding `--no-textconv` to `git show ref:path`.** The approver decided
  against it (decision 3).

## Risks

- **Diff views lose textconv rendering.** Binary and LFS-pointer files show
  raw, or as "Binary files differ", in the review UI, `--numstat` (`-` instead
  of line counts) and the security gate input. The approver accepted this
  (decision 5). This affects the web-server pod and the control plane.
- **`--numstat` additions/deletions for files with a textconv driver** become
  `-`. The `--numstat` parser in `worktree_merge.py` (2288-2297) already maps
  `-` to 0, so it keeps working. It is the only `--numstat` call. The
  `--stat`/`--shortstat` callers read totals, so a binary file's lines drop
  out of those totals.
- **Security gate input changes.** `security_pre_merge_gate_sync` now scans
  the raw diff instead of textconv output. That is the intent's goal: a
  textconv can no longer hide content from the gate. A repo that relied on
  textconv to make a binary scannable loses that.
- **Scan-test false positives or negatives.** The matcher keys on string
  constants. A diff argv built only from variables, for example
  `args = [sub, ...]` with `sub = "diff"`, escapes it. The five-site
  assertion and code review cover this. A list of command names such as
  `project/command_registry/base.py:49` does not match, because no `"git"`
  precedes `"diff"` there and it is not a git-call argument.
- **`git show <commit>` prints a patch by default** and is not flagged by the
  scan. No such call exists today. Approver decision 3 keeps `show` out of
  scope unless it carries `-p`/`--patch`.
- **The three backend full-patch sites** (`insight_extractor.py:91`,
  `context_gatherer.py:559`, `modification_tracker.py:229`) pass no `env=`.
  They are not in `test_no_unscrubbed_spawn.py`'s scanned set, and this
  change leaves their env handling alone. The intent's "stay on
  `child_env()`" constraint is met by not regressing the sites that already
  use it. Adding `env=` here is out of scope.
- **Merge conflict with PR #1748** in `core/worktree.py` if that PR also
  edits `_run_git` call sites. This change only edits argv literals, so a
  rebase conflict stays small. It avoids the three files #1748 owns.

## Verification

- `pytest tests/test_diff_no_ext_textconv.py -q`: the scan finds 0 offenders
  and the five full-patch sites are present. The behaviour test passes for
  textconv, `diff.<x>.command` and `diff.external`, and the control run shows
  the plant fires without the flags.
- Before the call-site edits, the scan test fails and lists every site from
  part 1. This shows that the matcher works.
- `pytest tests/test_command_sandbox.py tests/test_security.py -q`: the new
  textconv payloads are blocked and the legitimate commands still pass.
- `pytest tests/test_no_unscrubbed_spawn.py -q` stays green.
- The existing tests for the touched modules stay green: the plan lists them
  (worktree, worktree_merge routes, merger, pr_endgame, trusted_plan,
  timeline/file_evolution, insight_extractor, workspace commands). Run the full
  `pytest tests -q` before the PR.
- `ruff check` and `ruff format --check` on the touched files.
- Manual check: on a dev server, open a task diff in a repo with a planted
  `diff.x.textconv` that touches a marker. The diff renders, and no marker
  appears.
