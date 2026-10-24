---
status: draft
issue: Factory#1963
intent: intent/2026-10-24-1963-verdict-in-pr-evidence.md
---

# Spec: record the verify verdict and its vote split as PR evidence

## Design

The verdict only exists at merge time, per head commit. The PR body is written
before verify has run. So the evidence is written by the merge gate, into a
marked section of the PR body, each time the rendered evidence changes.

1. **Keep the vote summary.** `tfactory_verdict`
   (`apps/backend/pfactory/tfactory_client.py:901`) gets one optional keyword,
   `vote: dict | None = None`. When the function returns a verdict that is not
   `None`, and `verdicts.json` has a `verdict_vote` dict, it copies that dict into
   `vote`. The return value and every other behaviour stay the same. An
   out-parameter, so every caller and every test stub
   (`lambda *_a, **_k: "pass"`) keeps working.

2. **Render (pure).** A new `verify_evidence_block(signals, vote, *, head,
   allowed)` in `pr_endgame.py` returns the section as text, between the markers
   `<!-- aifactory:verify-evidence -->` and `<!-- /aifactory:verify-evidence -->`:

   ```
   ### Verify evidence
   - Verdict: pass (TFactory, model-assisted) at `0123abcd4567`
   - Votes: 3-0 x4, 2-1 x1 (3 calls, 0 failed)
   - Floor: host CI green: yes; VAL: VAL-2 (floor VAL-2): met; CI parity: yes
   - Merge gate: allowed
   The verdict is recorded as evidence. The deterministic floor decides: a pass
   cannot lift a red floor.
   ```

   - Verdict: `pass` when `merge_policy._verdict_pass` is true, `fail` for
     `"fail"` or `"handback"`, `hold` for anything else (no trusted result).
   - Votes: from `vote["splits"]`, keys matching `^\d+-\d+$` with int values,
     sorted by key. `calls` and `failed_calls` only if they are ints. With no
     valid split: `Votes: not reported`.
   - Floor: `host_ci_green` and `ci_parity` as yes or no. VAL shows each level
     as `VAL-<n>` from `merge_policy._val_rank`, which parses ints and strings
     like `"VAL-2"`. An unparseable achieved level shows `not recorded`, and an
     absent floor shows `none declared`. Then `met` or `not met` from
     `merge_policy._val_meets_floor`, the same function `decide_merge` uses.
   - Head: shown only if it is 7-40 hex characters, cut to 12.
   - Nothing else from TFactory or metadata reaches the text.

3. **Write (best-effort).** A new `write_verify_evidence(owner, repo, pr, block,
   *, runner)` reads the body (`gh pr view <pr> --repo o/r --json body --jq
   .body`), replaces the marked section or appends it, and writes it back with
   `gh pr edit <pr> --repo o/r --body <text>` (the `--body` argv shape
   `create_pr` uses, `pr_endgame.py:722`). It never raises and
   returns a bool.

4. **Wire it.** In `_build_merge_gate` (`pr_endgame.py:1817`), `fetch` passes
   a `vote` dict to `tfactory_verdict` and keeps its last result in the
   closure. After the decision, `gate` builds
   `merge_gate_signals(spec_dir, fetched=last)` and renders the block. It
   calls `write_verify_evidence` only when the block differs from the last one
   written, and then returns the same bool as before. The gate needs the PR
   number, so `run_pr_endgame` builds the gate after `create_pr` returns
   (`pr_endgame.py:1986-2001` moves below `:2027`), passing `pr`.

Only auto-merge PRs get the section, because only they have a merge gate. That
is where the verdict is a merge input. A PR without auto-merge does not use the
verdict, so there is nothing to record.

## Alternatives rejected

- **Write it into the PR body at PR open.** The verdict does not exist yet, so
  the section would always say hold.
- **A PR comment per change.** The gate polls every 30 s. A comment per change
  of head or CI state makes a thread of near-duplicates. One section that is
  rewritten in place is the "header" the issue asks for.
- **A new `tfactory_verdict_detail` returning a tuple.** It is cleaner, but the
  gate would have to call it instead of `tfactory_verdict`. That breaks the
  existing stubs in `test_pr_endgame_merge_gate.py:718-720` and the other merge
  tests that patch `tfactory_verdict`.
- **A second HTTP read of `verdicts.json` for the vote.** Two fetches can see two
  different documents. One fetch gives one consistent record.
- **The PFactory artifact header** (`plan/emit/docs/render.py`). That header is
  rendered before verify runs, and no verdict reaches PFactory. This was already
  rejected in `plan/2026-10-24-1963-verify-floor-proof.md`.

## Risks

- **Real `gh` in tests.** Tests that drive `run_pr_endgame` without a fake
  runner would run `gh pr view/edit`. Mitigation: an autouse conftest fixture
  stubs `pe.write_verify_evidence`. The test of the writer calls the real
  function with a fake runner.
- **Body race.** A human edit between the read and the write is lost. The window
  is one `gh` round trip. The write happens only when the evidence changes,
  which is a few times per PR.
- **Moving the gate build** after `create_pr` must not change which PRs get a
  gate. `auto_merge` and `human_approval_required` are settled before
  `create_pr` and are not touched again.
- **Untrusted input in the PR body.** TFactory's HTTP payload and agent-written
  metadata. Mitigated by the validation in design point 2, and tested.

## Verification

- Unit tests of `verify_evidence_block`: pass with splits, fail, hold
  (`None`), handback shown as fail, no `verdict_vote` gives "votes: not
  reported", a hostile split key and a non-int VAL are dropped, the floor line
  for red host CI.
- Unit tests of `tfactory_verdict` with a `vote` dict: filled on a pass, left
  empty on an untrusted result, and the return values do not change.
- Unit tests of `write_verify_evidence` with a fake runner: append, replace in
  place, a failing `gh` returns False and does not raise.
- A gate test: one `run_pr_endgame` with auto-merge writes the section once
  across several polls with the same evidence, and the merge result is the same
  as without it.
- The existing `-k 1963` test and the full merge-gate and endgame test files
  stay green, with `-o asyncio_mode=auto`.
