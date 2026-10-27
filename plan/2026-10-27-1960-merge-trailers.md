---
status: draft
issue: 1960
spec: spec/2026-10-27-1960-merge-trailers.md
---

# Plan: agent identity trailers on the commits that reach the default branch

Approved decisions (from the spec):

- Same default-off flag `AIFACTORY_AGENT_COMMIT_TRAILERS` and the same value
  source/validation as #1907 (`core/commit_trailers.py`). Flag off or no valid
  values → every argv is exactly today's.
- New `trailer_block(spec_dir: Path | None) -> str` in
  `apps/backend/core/commit_trailers.py`: `"\n".join(commit_trailer_args(spec_dir)[1::2])`
  (the `Key: value` items), `""` when none. Never raises.
- Values come only from the canonical spec dir
  `<project>/.aifactory/specs/<spec_id>` in the main checkout, never the
  worktree copy.
- Local merge (`core/worktree.py` `merge_worktree`): when committing, append
  `-m <block>` after the existing `-m "aifactory: Merge <branch>"`.
- Approve-button squash (`server/services/approval.py` `merge_pull_request`)
  and PR-endgame squash (`server/services/pr_endgame.py` `merge_pr`, both
  `gh pr merge` calls): append `--body <block>`. This replaces GitHub's default
  squash body, so agent-typed trailer lines in branch commits do not reach the
  default branch.
- No commit-msg hook in the agent session (`core.hooksPath=/dev/null` is a
  deliberate pin; a client-side hook is bypassable). No `--subject`. The generic
  `routes/github.py` PR merge (no task context) is out of scope.

## Steps

1. `apps/backend/core/commit_trailers.py`: add `trailer_block` (one-line
   docstring) after `commit_trailer_args`. `tests/test_commit_trailers.py`:
   tests for flag off → `""`; flag on with all three values →
   `"Agent-Identity: …\nAutonomy-Tier: …\nOriginator: …"`; one value missing →
   two lines; `spec_dir=None` → `""`. Reuse the file's existing helpers/fixtures.
   → verify: `pytest tests/test_commit_trailers.py -q`
   Traps: none.

2. `apps/backend/core/worktree.py` ~line 1142-1147 in `merge_worktree`: in the
   `else` branch (not `no_commit`), after the existing `-m`, compute
   `block = trailer_block(self.project_dir / ".aifactory" / "specs" / spec_name)`
   and `merge_args.extend(["-m", block])` only if `block`. Import
   `trailer_block` next to the existing `core.commit_trailers` import in that
   file. Add to `tests/test_worktree_merge_dirty.py` (reuse `temp_git_repo`,
   `_git`, the create/commit/merge pattern of the existing test) two real-git
   tests: flag on (`monkeypatch.setenv`) with
   `temp_git_repo/.aifactory/specs/001-feature/requirements.json` holding
   `agent_identity`, `intake.autonomy_tier`, `originator` → after
   `merge_worktree(..., no_commit=False)`, `git log -1 --format=%(trailers)` on
   base contains the three lines and the subject is still
   `aifactory: Merge <branch>`; flag off (`delenv`) with the same file → no
   trailers and the full message is exactly `aifactory: Merge <branch>`.
   → verify: `pytest tests/test_worktree_merge_dirty.py tests/test_worktree_security_gate.py tests/test_workspace.py -q`
   Traps: do not touch `pins`, the stash logic or the `no_commit` branch. If
   `.aifactory/` is not ignored in the fixture repo, make sure the untracked
   requirements.json does not block the merge (write it after creating the
   worktree; it is untracked in base and absent from the branch).

3. `apps/web-server/server/services/approval.py` `merge_pull_request`
   (line ~121): add keyword `spec_dir: Path | None = None`; build
   `argv = ["pr", "merge", str(number), "--squash"]`, then
   `if body := trailer_block(spec_dir): argv += ["--body", body]`; import
   `from core.commit_trailers import trailer_block` (web-server already imports
   `core.*` at module level, e.g. `services/agent_service.py:44`). Docstring:
   one line on `spec_dir`. Caller `apps/web-server/server/routes/worktree_merge.py:1962`:
   pass `spec_dir=spec_dir` (the canonical one built at ~line 1893). Tests in
   the web-server tests dir (new `apps/web-server/tests/test_merge_trailers.py`
   is fine): monkeypatch `approval._open_pr` → `(7, "OPEN")` and
   `approval.run_gh_command` to record argv; flag on + requirements.json in
   `tmp_path` → argv ends with `--body` and the block; flag off → argv exactly
   `["pr", "merge", "7", "--squash"]`.
   → verify: `cd apps/web-server && pytest tests/test_merge_trailers.py tests/test_worktree_branch_as_truth.py -q` (use the repo's usual web-server pytest invocation)
   Traps: approval.py imports lazily in places; keep module conventions.

4. `apps/web-server/server/services/pr_endgame.py`:
   `merge_pr` (line 1299) gains keyword `body: str | None = None`; add
   `["--body", body]` to `extra` when `body` so both `gh pr merge` calls carry
   it. `watch_and_finish` (line 1527) gains `merge_body: str | None = None` and
   passes `body=merge_body` in its `merge_pr` call (~line 1677).
   `run_pr_endgame` passes `merge_body=trailer_block(spec_dir) or None` to
   `watch_and_finish` (~line 2208); import `trailer_block` the way step 3 does.
   Tests (same new file or `test_pr_endgame.py`, following its fake-runner
   pattern): `merge_pr(..., body="Agent-Identity: x")` → runner argv contains
   `--body`, `Agent-Identity: x`; without body → argv unchanged from today;
   `watch_and_finish(..., merge_body=…)` forwards it (monkeypatch
   `pe.merge_pr` to capture kwargs, using an existing approved-and-merges test
   as the template).
   → verify: `cd apps/web-server && pytest tests/test_pr_endgame.py tests/test_merge_trailers.py -q`
   Traps: the `--match-head-commit` binding (#1663) must stay; keep `noqa`
   comments on the signatures (PLR0913 etc.) and add to them if ruff asks.

5. Checks: `ruff check` and `ruff format --check` on every touched file;
   `scripts/cq_ratchet.py --paths <touched files>` shows no regressions. If CI's
   autonomy-matrix check fails because of the new imports, regenerate with
   `scripts/gen_autonomy_matrix.py` (as #1907 did) and commit the two generated
   files.
   → verify: all clean.
   Traps: never `git add -A`; stage named paths only; never `--no-verify`.

## Tests

- `pytest tests/test_commit_trailers.py tests/test_worktree_merge_dirty.py tests/test_worktree_security_gate.py tests/test_workspace.py -q`
- `cd apps/web-server && pytest tests/test_merge_trailers.py tests/test_pr_endgame.py tests/test_worktree_branch_as_truth.py -q`
- New tests fail without the code (missing helper / no trailers / no `--body`).

## Rollback

Unset the flag (default) for an immediate no-op; otherwise revert the PR. No
data, schema or config.
