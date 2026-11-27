---
status: approved
issue: 2258
spec: spec/2026-11-26-2258-tf-writeback-head.md
coders:
  1: codex   # pure refactor with the existing tests as the oracle
  2: codex   # small, security-relevant resolver with exact rule tests
  3: codex   # one guarded branch in qa_gate; same context
  4: codex   # one-line wiring plus a wiring test; same context
reviewers:
  - codex    # fail-closed paths and the trust argument against the spec
  - agy      # independent read of the resolver and the qa_gate branch
---

# Plan: accept TFactory's own test write-back commit as the verified head

## Approved decisions (from the spec)

**The resolver.** `writeback_base(spec_dir, head)` returns TFactory's `source_sha` only when `head` is
TFactory's own test write-back directly on it. All of these must hold:

- the TFactory task is project-checked and `triaged`;
- `git_writer` has `ok is True`, `dry_run is False` and `commit_sha == head`;
- GitHub reports exactly one parent, and it equals `source_sha`;
- every changed file (including any `previous_filename`) is in `committed_paths` and is
  test-shaped, with fewer than 300 files.

Otherwise it returns `None`. It never raises.

**Where it is used.**

- **`qa_gate.check`** uses it on a `reviewed_head` mismatch. That covers every caller, including
  CFactory approve.
- **The merge gate** asks for the TFactory verdict at `writeback_base(...) or head`.
- **CI and the merge pin** stay on `head`.

## Steps

1. **`apps/backend/pfactory/tfactory_client.py`.**
   - Extract `tfactory_task(spec_dir, spec_id, *, getter=None) -> dict[str, Any] | None` from
     `tfactory_verdict` (around lines 1295–1321). It covers the config, the project id, the
     scoped URL and headers, the GET, the 2xx check, the dict check and the `project_id`
     equality. It never raises.
   - `tfactory_verdict` calls it, then keeps its `source_sha`, `triaged` and `verdicts.json`
     logic. The `verdicts.json` URL still needs `base` and `qs`, so return them from a private
     `_task_request(spec_dir, spec_id)` that both functions use.

   Verify: `cd apps/backend && /mnt/code/Source-home/GitHub/AIFactory/apps/backend/.venv/bin/pytest pfactory/test_tfactory_verdict.py -q`.
   All existing tests pass unchanged. Add `tfactory_task` tests for a match, a mismatch, a non-2xx
   response and unconfigured.

   Traps:
   - the ruff ratchet (PLR2004, PLR0913, BLE001 with a reason) and mypy on the tests.

2. **New `apps/web-server/server/services/tf_writeback.py`.**
   - `writeback_base(spec_dir, head, *, getter=None, reader=None) -> str | None`, exactly as in the
     spec.
   - `_is_test_path(path) -> bool`.
   - `_github_repo(project_dir) -> tuple[str, str] | None`, which parses
     `https://github.com/o/r(.git)` and `git@github.com:o/r(.git)`.
   - `_gh_commit(owner, repo, sha, cwd) -> tuple[list[str], list[str]] | None`. It runs
     `gh api repos/{o}/{r}/commits/{sha}` through `subprocess` under
     `project_github_env(cwd, child_env())`, the same imports as `pr_endgame._default_runner`,
     with a 30 s timeout. It parses `parents[].sha` and `files[].filename` / `previous_filename`.
   - Lazy-import `pfactory.tfactory_client` inside the function, as `pr_endgame` does.

   Tests go in the new `apps/web-server/tests/test_tf_writeback.py`, covering every accept and
   reject case in the spec.

   Verify: from the repo root,
   `PYTHONPATH=apps/backend /mnt/code/Source-home/GitHub/AIFactory/apps/backend/.venv/bin/pytest apps/web-server/tests/test_tf_writeback.py -q`.

   Traps:
   - an S603 / S607 noqa for `subprocess` and `gh`, matching the existing style;
   - `_project_dir` lives in `tfactory_client`, so reuse it.
3. **`apps/web-server/server/services/qa_gate.py` `check`.**
   - In the `reviewed_head` comparison, a mismatch first tries
     `writeback_base(source_spec_dir, head_sha)` through a lazy import;
   - if that equals `reviewed_head.strip()`, it falls through to the remaining checks; otherwise it
     returns `qa_stale`;
   - the exact match never calls it.

   Add tests in the existing qa_gate test file: find it with `grep -rl "qa_gate" apps/web-server/tests`.
   Monkeypatch `tf_writeback.writeback_base`.

   Verify: `PYTHONPATH=apps/backend …/pytest apps/web-server/tests -q -k qa_gate`.

   Traps:
   - `check` already carries `noqa: PLR0911, PLR0912`; adding a branch may trip PLR0915 or C901.
     Keep it inside a small helper, `_head_matches(spec_dir, reviewed, head)`.
4. **`apps/web-server/server/services/pr_endgame.py` `_build_merge_gate.fetch`** (around line
   2157).
   - `verified = writeback_base(spec_dir, head) or head` (lazy import);
   - `tfactory_verdict(..., head=verified, ...)`;
   - CI calls keep `head`.

   Add a wiring test in `apps/web-server/tests/test_pr_endgame_merge_gate.py`: a stubbed
   `writeback_base` returning the base means `tfactory_verdict` is called with the base, and
   `host_ci_green` with the head.

   Verify: `PYTHONPATH=apps/backend …/pytest apps/web-server/tests -q -k "endgame or merge_gate or qa_gate or writeback"`.

## Tests

- Every verify command passes, with no new failures against an `origin/dev` baseline.
- CI is green: the ruff, mypy and pytest ratchets.
- Live: release AIFactory 3.21.18 and rerun E2E P1. S4 merges and S5 runs. CFactory approve on run
  `n`'s task no longer gives `qa_stale`.

## Rollback

Revert the PR. Both gates go back to an exact head match: write-back runs stall again, but nothing
is merged wrongly.

## Deviations

qa_gate.check is called via asyncio.to_thread in async callers (pr_endgame._gated_head, routes/pr.py) because it may now perform network I/O (Fable review).
