---
status: draft
issue: Factory#1963
spec: spec/2026-10-24-1963-verify-floor-proof.md
---

# Plan: prove a passing verdict cannot merge past a red floor

Approved decisions (from the spec):

- Test only, in `apps/web-server/tests/test_pr_endgame_merge_gate.py`. Change
  production code only if a red case merges, and then update this plan in the
  same commit.
- Drive the real path: `pe.run_pr_endgame(..., background=False)` builds
  `_build_merge_gate` (`apps/web-server/server/services/pr_endgame.py:1817`),
  wraps it in `_with_receipt_veto` (`:1798`), and awaits the real
  `watch_and_finish` (`:1400`). Only external edges are stubbed.
- TFactory always returns `"pass"`, which stands in for the permissive model
  verdict. The review is approved.
- Four cases: `host_ci_red`, `val_below_floor`, `ci_parity_false`, and the
  `all_green` control.
- The VAL and parity metadata is written inside the `create_pr` stub, after the
  PR-open decision. This makes every red case reach the merge-time gate with
  `auto_merge=True`.
- Red: `merge_pr` is never called, `merged is False`, and
  `reason == "merge_gate_timeout"`. Control: `merge_pr` is called once with
  `match_head="deadbeef"`, and `merged is True`.
- A mutation check by hand on `decide_merge`, not committed.
- The PFactory header slice is skipped. No verify verdict or vote data reaches
  `plan/emit/docs/render.py`, and the header is rendered before any verify
  runs. It needs a TFactory-to-PFactory callback, which belongs in its own
  issue.
- Line correction: the spec cites `merge_policy.py` lines 307/309/310 for the
  conjuncts. On `origin/dev` at `2a9e9ea2` they are `host_ci_green` at 306,
  `_val_meets_floor(...)` at 308 and `ci_parity` at 309, inside the `if` at
  305-310.

## Steps

1. `apps/web-server/tests/test_pr_endgame_merge_gate.py`, append after the last
   test (the file ends at line 679). Add a section banner in the file's style:
   `# #1963: a passing verdict cannot merge past a red deterministic floor`.
   Then add one test,
   `test_1963_a_pass_verdict_cannot_merge_past_a_red_floor`, with
   `@pytest.mark.parametrize(("host_ci", "late_meta", "merges"), [...], ids=[...])`
   over:
   - `host_ci_red`: `(False, None, False)`
   - `val_below_floor`: `(True, {"achievedVal": "VAL-1", "valFloor": "VAL-3"}, False)`
   - `ci_parity_false`: `(True, {"ciParity": False}, False)`
   - `all_green`: `(True, None, True)`

   Body:
   - `spec = _spec(tmp_path)`. This is the existing helper (line 55). Pass no
     meta, so no metadata file exists when the PR opens.
   - `calls: list[object] = []`. Write a `_merge(*_a, **k)` spy that appends
     `k.get("match_head")` and returns `True`, then
     `monkeypatch.setattr(pe, "merge_pr", _merge)`.
   - Write a `_create_pr(**_k)`. When `late_meta` is set, it writes
     `json.dumps(late_meta)` to `spec / "task_metadata.json"`. It returns `42`.
     Set it with `monkeypatch.setattr(pe, "create_pr", _create_pr)`.
   - `monkeypatch.setattr(pe, "_pr_title_body", lambda _d, _s: ("t", "b"))`
   - `monkeypatch.setattr("pfactory.tfactory_client.tfactory_verdict", lambda *_a, **_k: "pass")`
   - `monkeypatch.setattr(pe, "host_ci_green", lambda *_a, **_k: host_ci)`
   - `monkeypatch.setattr(pe, "required_checks_green", lambda *_a, **_k: True)`
   - `monkeypatch.setattr(pe, "_pr_head", lambda *_a: "deadbeef")`
   - `real = pe.watch_and_finish`. Patch `pe.watch_and_finish` with a wrapper
     that returns `real(**kw, poll_interval=0, max_minutes=1)`.
   - `result = await pe.run_pr_endgame(spec_dir=spec, spec_id="001-x",
     worktree=spec, branch="aifactory/001-x", base="main", repo="owner/repo",
     auto_merge=True, review_tier="auto", reviewer="aifactory",
     review_fn=lambda: pe.ReviewState("approved"), background=False,
     trusted=None)`
   - When `merges`: `calls == ["deadbeef"]` and `result["merged"] is True`.
     Otherwise: `calls == []`, `result["merged"] is False`, and
     `result["reason"] == "merge_gate_timeout"`.
   - One-line docstring: the permissive model verdict is evidence, and the
     floor decides (Factory#1963).

   → verify by
   `apps/backend/.venv/bin/pytest apps/web-server/tests/test_pr_endgame_merge_gate.py -q -o asyncio_mode=auto -k 1963`.
   Expect 4 passed.

   Traps:
   - `-o asyncio_mode=auto` is required. Without it every async test in the
     file fails. CI passes it (`.github/workflows/ci.yml:218`).
   - Do not reuse `_endgame_auto_merge` (line 63), because it stubs out
     `watch_and_finish`.
   - Patch `tfactory_verdict` on the `pfactory.tfactory_client` module. The
     gate imports it inside `fetch` (`pr_endgame.py:1833`). If the import fails,
     the verdict is `None` and the red cases pass for the wrong reason. The
     control case catches this, so it must stay.
   - `run_pr_endgame` does not pass `poll_interval` or `max_minutes`, so the
     wrapper adds no duplicate keyword.
   - The conftest autouse `_no_handback_receipt` already stubs
     `hrs.on_record` to `False`, and the file's `_isolated_host` fixture applies
     too. Add no fixtures.
   - Run the worktree with the main clone's venv:
     `/mnt/code/Source-home/GitHub/AIFactory/apps/backend/.venv/bin/pytest`.
   - Keep the stubs' `_`-prefixed unused args, so ruff ARG rules stay quiet.

2. Mutation check by hand, not committed. In
   `apps/backend/merge/merge_policy.py:305-310`, delete one conjunct at a time
   and run the `-k 1963` command after each:
   - remove `host_ci_green and` (306): `host_ci_red` fails
   - remove `and _val_meets_floor(achieved_val, val_floor)` (308):
     `val_below_floor` fails
   - remove `and ci_parity` (309): `ci_parity_false` fails

   Then `git checkout -- apps/backend/merge/merge_policy.py`. That restores
   one file in the lane worktree; never run it in the main clone.
   → verify by `git status --short` showing only the test file changed.
   Put the three results in the PR body.
   Traps: never commit a mutated `merge_policy.py`.

3. Lint and the full file.
   → verify by `ruff check apps/web-server/tests/test_pr_endgame_merge_gate.py`,
   `ruff format --check apps/web-server/tests/test_pr_endgame_merge_gate.py`,
   and the full-file pytest command (expect 46 passed).
   Traps: hooks can take 10 minutes or more, so use long timeouts. Never pass
   `--no-verify` on this code commit.

## Tests

- `apps/backend/.venv/bin/pytest apps/web-server/tests/test_pr_endgame_merge_gate.py -q -o asyncio_mode=auto`
  gives 46 passed (42 now plus 4 new).
- Each step 2 mutation fails exactly its matching case.

## Rollback

Revert the test commit. There is no runtime effect.
