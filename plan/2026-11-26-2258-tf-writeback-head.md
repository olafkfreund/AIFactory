---
status: draft
issue: 2258
spec: spec/2026-11-26-2258-tf-writeback-head.md
coders:
  1: codex   # small, security-relevant logic in the verdict check, with exact tests
  2: codex   # the same task's caller and gh helper; keeps one implementer's context
reviewers:
  - codex    # checks the fail-closed paths and the parent check against the spec
  - agy      # independent read of the trust argument
---

# Plan: accept TFactory's own test write-back commit as the verified head

## Approved decisions (from the spec)

- **Two SHAs are accepted.** `tfactory_verdict` trusts a verdict when `source_sha == head`, as
  today. It also trusts it when all of these hold:
  - `status_json.git_writer` is a dict with `ok is True`, `dry_run is not True` and
    `commit_sha == head`;
  - a caller-supplied `head_parent(head)` returns exactly `source_sha`.
- **No callable, no write-back path.** Without `head_parent`, the write-back path is off.
  `head_parent` raising or returning `None` gives `None`.
- **The endgame supplies the parent.** It passes `_commit_parent(owner, name, sha, runner)`: a
  `gh api /repos/{o}/{r}/commits/{sha}` read that returns the parent only when there is exactly
  one, and otherwise `None`. It never raises.
- **No TFactory change.**

## Steps

1. `apps/backend/pfactory/tfactory_client.py`:
   - add `_verified_at(task, status_json, head, head_parent)` above `tfactory_verdict`;
   - add the keyword-only `head_parent: Callable[[str], str | None] | None = None` to
     `tfactory_verdict`;
   - in the task check, replace `and bool(head) and task.get("source_sha") == head` with a call
     to `_verified_at(...)`, placed after `isinstance(status_json, dict)`;
   - update the docstring.

   Tests go in `apps/backend/pfactory/test_tfactory_verdict.py`, covering the spec's table.

   Verify: `cd apps/backend && /mnt/code/Source-home/GitHub/AIFactory/apps/backend/.venv/bin/pytest pfactory/test_tfactory_verdict.py -q`.

   Traps:
   - `Callable` may need importing (check the existing `typing` / `collections.abc` imports);
   - the ruff ratchet applies (PLR2004 magic numbers; ARG001);
   - mypy runs on the tests: type the helpers.
2. `apps/web-server/server/services/pr_endgame.py`:
   - add `_commit_parent(owner, repo, sha, runner) -> str | None` after `_pr_head` (around line
     1191), in the same style: `runner([...], None)`, `res.ok`, and a broad `except` that returns
     `None`;
   - in `_build_merge_gate.fetch` (around line 2157), pass
     `head_parent=lambda sha: _commit_parent(owner, name, sha, runner)`.

   Tests go in the nearest endgame test file that stubs `runner` (for example
   `apps/web-server/tests/test_pr_endgame*.py`): one parent, zero, two, `ok=False`, a raise.

   Verify: from the repo root,
   `PYTHONPATH=apps/backend /mnt/code/Source-home/GitHub/AIFactory/apps/backend/.venv/bin/pytest apps/web-server/tests -q -k "endgame or merge_gate"`.

   Traps:
   - fake runners in the existing tests may route by argv; check that they don't break on the
     new call. It is only reached when the SHAs differ and `git_writer` matches.

## Tests

- Both verify commands pass, with no new failures against the `origin/dev` baseline.
- CI is green on the PR (ruff, mypy and pytest ratchets).

## Rollback

Revert the PR. The gate goes back to an exact SHA match: write-back runs stall at S4 again, but
nothing is merged wrongly.
