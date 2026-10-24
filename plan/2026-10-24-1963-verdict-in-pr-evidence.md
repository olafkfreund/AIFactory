---
status: approved
issue: Factory#1963
spec: spec/2026-10-24-1963-verdict-in-pr-evidence.md
---

# Plan: record the verify verdict and its vote split as PR evidence

Base: `origin/dev` at `717feb4b`. Branch `fix/1963-verdict-in-pr-evidence`.
AIFactory only. Line numbers below are for that base.

## Approved decisions (from the spec)

- The "artifact header" is a marked section of the PR body that the
  **merge gate** writes, between `<!-- aifactory:verify-evidence -->` and
  `<!-- /aifactory:verify-evidence -->`. It is not written at PR open, because
  the verdict does not exist yet then. It is not a PR comment, because the gate
  polls every 30 s.
- Contents: verdict `pass` / `fail` / `hold`; the vote split from TFactory's
  `verdict_vote.splits` (for example `3-0 x4, 2-1 x1`) or `Votes: not
  reported`; floor signals host CI green, VAL achieved against the floor, CI
  parity; the gate's own result; and the line "The verdict is recorded as
  evidence. The deterministic floor decides: a pass cannot lift a red floor."
- Verdict mapping: `merge_policy._verdict_pass(v)` gives `pass`; `"fail"` or
  `"handback"` gives `fail`; anything else (`None`, no trusted result) gives
  `hold`.
- The vote summary comes out of the fetch the gate already does: a new optional
  out-parameter `vote: dict | None = None` on `tfactory_verdict`. It is filled
  only when the return value is not `None`. There is no second HTTP read, and no
  new function the gate would call instead. Existing stubs
  `lambda *_a, **_k: "pass"` keep working and give "not reported".
- Write only when the rendered block differs from the last block this gate
  wrote. A write failure never raises and never changes the gate's bool.
- Only auto-merge PRs get the section, because only they build a merge gate.
- Untrusted input (TFactory HTTP, agent-written metadata) is never echoed.
  Split keys must match `^\d+-\d+$` with int values. `calls` and
  `failed_calls` must be ints (not bool). VAL goes through
  `merge_policy._val_rank` and is rendered `VAL-<n>`. Head must be 7-40 hex
  characters and is cut to 12.
- Rejected: PR-open body text, a PR comment per change, a
  `tfactory_verdict_detail` tuple function (it breaks the existing stubs), a
  second `verdicts.json` read, and the PFactory header (no verdict reaches
  PFactory).

## Steps

1. `apps/backend/pfactory/tfactory_client.py:901-953`: add keyword-only
   `vote: dict[str, Any] | None = None` to `tfactory_verdict`. Keep `doc` in
   scope. Replace the last line (`:953`) with:
   ```python
   verdict = "fail" if "reject" in kinds else "pass"
   summary = doc.get("verdict_vote")
   if vote is not None and isinstance(summary, dict):
       vote.update(summary)
   return verdict
   ```
   Add one docstring line: "``vote``, if given, receives TFactory's
   ``verdict_vote`` summary (Factory#1963)."
   Add tests to `apps/backend/pfactory/test_tfactory_verdict.py` (156 lines,
   reuse `_task`, `_verdicts`, `_run`, `TASK`). Cover: a pass doc with
   `"verdict_vote": {"splits": {"3-0": 2}}` fills the dict and still returns
   `"pass"`; an untriaged task leaves the dict empty and returns `None`; a doc
   with no `verdict_vote` leaves it empty. `_run` builds the getter, so either
   extend `_run` with `vote=None` passed through, or call `tc.tfactory_verdict`
   directly in the new tests with a local getter.
   → verify by
   `/mnt/code/Source-home/GitHub/AIFactory/apps/backend/.venv/bin/pytest apps/backend/pfactory/test_tfactory_verdict.py -q -o asyncio_mode=auto`
   (all pass, old tests unchanged).
   Traps: `kinds` is computed inside the `try`, and the final return is outside
   it, so `doc` is bound there only on the success path. That is fine, because
   every early exit returns first. Do not move the `verdict_vote` read into the
   `try`, or the `kinds` check would no longer guard it. Do not change the
   return type.

2. `apps/web-server/server/services/pr_endgame.py`, insert after
   `review_tier_note` (ends `:526`, before `def merge_disposition` at `:529`):
   - `_EVIDENCE_START = "<!-- aifactory:verify-evidence -->"` and
     `_EVIDENCE_END = "<!-- /aifactory:verify-evidence -->"`.
   - `verify_evidence_block(signals: Mapping[str, object], vote:
     Mapping[str, object] | None, *, head: str | None, allowed: bool) -> str`.
     It is pure and never raises (wrap it in `try` and fall back to a minimal
     block with `Verdict: hold`). It lazily imports `_verdict_pass`,
     `_val_rank` and `_val_meets_floor` from `merge.merge_policy`, the same
     lazy-import style as `merge_gate_signals` at `:415`. Output lines, in this
     order: start marker, `### Verify evidence`, `- Verdict: <v> (TFactory,
     model-assisted)` plus `` at `<head12>` `` when the head is valid,
     `- Votes: ...`, `- Floor: host CI green: yes|no; VAL: <a> (floor <f>):
     met|not met; CI parity: yes|no`, `- Merge gate: allowed|held`, the
     evidence line, end marker. `<a>` is `VAL-n` or `not recorded`; `<f>` is
     `VAL-n` or `none declared`. Votes: `", ".join(f"{k} x{n}")` over the valid
     splits sorted by key, then ` (<calls> calls, <failed> failed)` when both
     are valid ints; else `not reported`.
   - `write_verify_evidence(owner: str, repo: str, pr: int, block: str, *,
     runner: Runner = _default_runner) -> bool`. It reads with
     `runner(["gh", "pr", "view", str(pr), "--repo", f"{owner}/{repo}",
     "--json", "body", "--jq", ".body"], None)`. If both markers are present
     with start before end, it replaces from start through end with `block`.
     Otherwise it appends `"\n\n" + block`. It writes with
     `runner(["gh", "pr", "edit", str(pr), "--repo", f"{owner}/{repo}",
     "--body", new_body], None)`, returns `res.ok`, and catches every
     exception to return False. If the read fails, it returns False and does
     not write. Never write a body built from a failed read.
   → verify by
   `ruff check apps/web-server/server/services/pr_endgame.py` and
   `ruff format --check apps/web-server/server/services/pr_endgame.py`.
   Traps: the file logs through `sanitize_log`. Never log `block` or the body.
   `bool` is an `int` subclass, so exclude it when checking split counts,
   `calls` and `failed_calls`. Only fixed labels and validated numbers go into
   the text.

3. `pr_endgame.py:1817-1868` (`_build_merge_gate`): add a keyword-only
   parameter `*, pr: int | None = None` after `base`. With `pr is None`, no
   evidence is written. This keeps the nine-positional-argument calls in
   `apps/web-server/tests/test_trusted_contract_merge.py:743,798,820` working.
   Inside it, keep `state: dict[str, object] = {}` (holding
   `"fetched"`, `"vote"` and `"written"`). In `fetch()`, create `vote: dict = {}`
   and call `tfactory_verdict(spec_dir, spec_id, head=head, vote=vote)`. Store
   the returned dict and `vote` in `state` before returning. In `gate(head)`,
   compute the existing bool into `ok` (both branches, unchanged). Then, only if
   `"fetched" in state`, build
   `block = verify_evidence_block(merge_gate_signals(spec_dir,
   fetched=state["fetched"]), state["vote"], head=head, allowed=ok)`. If
   `block != state.get("written")` and
   `write_verify_evidence(owner, name, pr, block, runner=runner)` succeeds, set
   `state["written"] = block`. Wrap the evidence part in `try/except
   Exception` with a `logger.debug(..., exc_info=True)`. Return `ok`.
   → verify by the step 5 tests.
   Traps: reset `state` at the start of each `gate()` call, apart from
   `"written"`, so a gate call that never reached `fetch` (unreadable contract)
   does not render the previous head's data. The `ImportError` fallback at
   `:1839` must still set `verdict = None` and leave `vote` empty.

4. `pr_endgame.py:1986-2001` (`run_pr_endgame`): move the `merge_gate =
   (_build_merge_gate(...) if auto_merge else None)` and `_with_receipt_veto`
   lines to just after `if pr is None: return {...}` (`:2026-2027`), and pass
   `pr=pr`. `owner, name = parts` stays where it is
   (`:1984`). Nothing between the old and new spot reads `merge_gate`. Check
   with `grep -n merge_gate` that the only later use is
   `watch_and_finish(..., merge_gate=merge_gate)` (`:2064`).
   → verify by
   `/mnt/code/Source-home/GitHub/AIFactory/apps/backend/.venv/bin/pytest apps/web-server/tests/test_pr_endgame_merge_gate.py apps/web-server/tests/test_merge_decision_reaches_the_merge.py apps/web-server/tests/test_trusted_contract_merge.py apps/web-server/tests/test_pr_endgame.py -q -o asyncio_mode=auto`
   (same pass count as before the change).
   Traps: `asyncio.get_running_loop()` in `_with_receipt_veto` is still inside
   the coroutine. Fine. `apps/web-server/tests/test_pr_endgame.py:45` stubs
   `_build_merge_gate` as `lambda *_a, **_k: ...`, which accepts `pr=`. The
   `tfactory_verdict` stubs in `test_trusted_contract_merge.py:738,819` and
   `test_merge_decision_reaches_the_merge.py:126` take `**_k`, so `vote=` is
   accepted.

5. Tests.
   - `apps/web-server/tests/conftest.py`, after `_no_handback_receipt`
     (`:44-55`): add an autouse fixture `_no_pr_evidence_write` that
     monkeypatches `server.services.pr_endgame.write_verify_evidence` to
     `lambda *_a, **_k: True`. Without it, endgame tests that use
     `_default_runner` would run real `gh`.
   - New file `apps/web-server/tests/test_pr_verify_evidence.py`:
     a. `verify_evidence_block` cases. A pass with
        `{"splits": {"3-0": 4, "2-1": 1}, "calls": 3, "failed_calls": 0}`
        renders `Votes: 2-1 x1, 3-0 x4 (3 calls, 0 failed)`. A `None` verdict
        renders `Verdict: hold`. `"handback"` renders `fail`. An empty vote
        renders `Votes: not reported`. A hostile key `"3-0\n@x"` and a bool
        count are dropped. `achieved_val="VAL-2"` with `val_floor=3` renders
        `VAL: VAL-2 (floor VAL-3): not met`. `host_ci_green=False` renders `no`.
        A non-hex head is omitted. Every block starts and ends with the markers
        and contains the evidence line.
     b. `write_verify_evidence` with a fake runner that records argv. Cases:
        append to a body with no markers; replace the old section in place so
        the text before and after is kept and one marker pair remains; a failed
        view returns False and makes no edit call; a runner that raises returns
        False.
     c. Gate wiring. Copy the setup of
        `test_1963_a_pass_verdict_cannot_merge_past_a_red_floor`
        (`test_pr_endgame_merge_gate.py:695-752`) for the `all_green` case,
        with `tfactory_verdict` stubbed as
        `def _v(*_a, vote=None, **_k): vote.update({"splits": {"3-0": 2}}); return "pass"`,
        and `pe.write_verify_evidence` re-patched to record its calls. Assert
        one call with `pr == 42`, the block contains `Verdict: pass` and
        `3-0 x2`, and the result has `merged is True`. Add a red host-CI case
        where `host_ci_green` flips False and then True. That gives two polls
        with different blocks, so two writes, and the first block says
        `Merge gate: held`.
   → verify by
   `/mnt/code/Source-home/GitHub/AIFactory/apps/backend/.venv/bin/pytest apps/web-server/tests/test_pr_verify_evidence.py -q -o asyncio_mode=auto`.
   Traps: `-o asyncio_mode=auto` is required. Reuse the merge-gate file's
   `_isolated_host` and `_spec` by importing them, or copy the three monkeypatch
   lines. An autouse fixture does not apply across files unless it is in
   conftest. Keep the stubs' `_`-prefixed unused args, so ruff ARG rules stay
   quiet. The `watch_and_finish` wrapper passes `poll_interval=0,
   max_minutes=1`. Keep that, or the test waits 30 s.

6. Full check and commit.
   → verify by
   `ruff check apps/backend/pfactory apps/web-server/server/services/pr_endgame.py apps/web-server/tests`,
   `ruff format --check` on the same paths,
   `/mnt/code/Source-home/GitHub/AIFactory/apps/backend/.venv/bin/pytest apps/web-server/tests -q -o asyncio_mode=auto`
   and
   `/mnt/code/Source-home/GitHub/AIFactory/apps/backend/.venv/bin/pytest apps/backend/pfactory apps/backend/merge -q -o asyncio_mode=auto`.
   Commit `fix(pr-endgame): record verify verdict and vote split as PR evidence (Factory#1963)`.
   Traps: hooks can take 10 minutes or more, so use long timeouts. Never pass
   `--no-verify` on a code commit. If you deviate from this plan, update this
   file in the same commit. The PR body states the box this ticks: "The
   verdict appears in the artifact header as recorded evidence with its vote
   split".

## Tests

- `pytest apps/backend/pfactory/test_tfactory_verdict.py -q -o asyncio_mode=auto`: all pass.
- `pytest apps/web-server/tests/test_pr_verify_evidence.py -q -o asyncio_mode=auto`: all pass.
- `pytest apps/web-server/tests -q -o asyncio_mode=auto`: no new failures
  compared with `origin/dev`.
- `pytest apps/web-server/tests/test_pr_endgame_merge_gate.py -k 1963 -q -o asyncio_mode=auto`:
  4 passed, unchanged.

## Rollback

Revert the single code commit. Nothing is persisted outside PR bodies, and a
leftover evidence section in an old PR is inert text.
