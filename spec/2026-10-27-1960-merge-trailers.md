---
status: approved
issue: 1960
intent: intent/2026-10-27-1960-merge-trailers.md
---

# Spec: agent identity trailers on the commits that reach the default branch

## Design

One new helper in `apps/backend/core/commit_trailers.py`:
`trailer_block(spec_dir: Path | None) -> str`, the `Key: value` lines of
`commit_trailer_args(spec_dir)` joined by `\n`, or `""` when there are none.
It reuses #1907's flag check and validation (printable, non-empty strings from
`requirements.json`), so the same values and the same off-switch apply. Every
merge path appends this block as the last paragraph of the merge commit
message only when it is non-empty; an empty block leaves the argv unchanged.

1. Local merge, `WorktreeManager.merge_worktree` (`core/worktree.py`): when it
   commits (not `no_commit`), append `-m <block>` (git joins several `-m` as
   paragraphs, so the block is the trailer paragraph). Spec dir:
   `self.project_dir / ".aifactory" / "specs" / spec_name`, the canonical copy
   in the main checkout, not the agent's worktree copy. No caller changes.
2. Approve button, `merge_pull_request(project_path, branch, spec_dir=None)`
   (`server/services/approval.py`): append `--body <block>` to
   `gh pr merge --squash`. The caller in `routes/worktree_merge.py` passes its
   `spec_dir` (`project_path / ".aifactory" / "specs" / spec_id`, canonical).
3. PR endgame auto-merge, `merge_pr(..., body=None)`
   (`server/services/pr_endgame.py`): append `--body <body>` to both
   `gh pr merge` calls (first try and post-update retry). `watch_and_finish`
   gains `merge_body: str | None = None` and forwards it; `run_pr_endgame`
   passes `trailer_block(spec_dir) or None` (its `spec_dir` is the canonical
   one from `completion_orchestration`).

Setting the squash body replaces GitHub's default body (the concatenated branch
commit messages) only when the flag is on and values exist. That is the point:
it is what keeps agent-typed trailer lines off the default branch.

## Alternatives rejected

- commit-msg hook in the task worktree. AIFactory pins `core.hooksPath=/dev/null`
  for all git it runs (`core/child_env.py`, #1689) because hooks in an
  agent-writable repo are an execution vector; re-enabling them reverses that.
  A client-side hook is also bypassable by the agent (`--no-verify`,
  `commit-tree`, `--amend`, editing `.git/hooks` or the `requirements.json` it
  reads in the worktree). Tamper-proof per-commit attribution needs a
  server-side identity (GitHub App per agent), a later slice.
- Server-side rewrite of agent commits (`interpret-trailers` + rebase before
  push): rewrites history the reviewer saw, breaks `--match-head-commit`
  binding (#1663) and the merge gate (#1695).
- `--subject` as well as `--body`: the default title (PR title + number) is
  fine and keeps the change minimal.
- Generic `POST /github/pr/{n}/merge` (`routes/github.py`): no task context to
  read values from; left as a remaining slice.

## Risks

- Flag on, squash body is only the trailers; the per-commit message list
  GitHub would have written is gone from the squash commit (still on the PR).
  Accepted; flag is default off.
- `gh pr merge --auto` (`enable_auto_merge`) is not used by these paths; not
  changed.
- Validation is #1907's: non-printable or non-string values are dropped, never
  written.

## Verification

- Unit: `trailer_block` off/on/partial; `merge_pull_request` and `merge_pr`
  argv with and without a body (flag off → exactly today's argv);
  `watch_and_finish` forwards `merge_body` to `merge_pr`.
- Real git: `merge_worktree` with the flag on and a canonical
  `requirements.json` → `git log -1 --format=%(trailers)` on the merge commit
  shows the three trailers; flag off → none, message unchanged.
- ruff, ruff format, `scripts/cq_ratchet.py` on touched files; CI green.
