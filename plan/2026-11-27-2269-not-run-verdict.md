---
status: approved
issue: 2269
spec: spec/2026-11-27-2269-not-run-verdict.md
coders:
  1: manager  # one-line logic change plus tests, below the handoff threshold (team policy)
reviewers:
  - codex    # plan review (team policy)
  - codex    # diff review: a provider other than the implementer, Claude (team policy)
---

# Plan: a `not_run` TFactory verdict no longer holds the merge gate forever

## Approved decisions

- In `tfactory_verdict` (`apps/backend/pfactory/tfactory_client.py`, ~1385), drop the
  `"not_run"` entries from `kinds` before the existing check:
  `if not kinds or any(k not in ("accept", "flag", "reject") for k in kinds): return None`.
- Everything else is unchanged:
  - `reject` gives `fail`, otherwise the result is `pass`;
  - an all-`not_run` run gives `None`;
  - any other unknown value gives `None`;
  - the vote summary is unchanged.
- The docstring notes that `not_run` rows are ignored.
- **Evidence (Codex review):** when `vote` is passed and at least one `not_run` row was dropped,
  `tfactory_verdict` sets `vote["not_run_excluded"] = n`. This happens even when TFactory sent
  no vote summary.
  - `pr_endgame._votes_text` appends `; N not run (excluded)` when that count is a positive
    integer, including when the splits read "not reported".
  - The PR evidence block then shows what the verdict skipped.
- **Adopted PRs (Codex review):** the `verdict_only` readiness path uses the same parser and
  advances on the remaining rows. This is deliberate: `not_run` is "no evidence", and that path
  never merges. A run where every row is `not_run` still gives `None`, so nothing advances.

## Steps

1. Edit `tfactory_client.py` as above.

   **Tests:** in `apps/backend/pfactory/test_tfactory_verdict.py`, a parametrized test using the
   existing `_run` and `_verdicts` helpers covers these cases:

   | Verdicts | Result |
   |---|---|
   | `accept`, `not_run` | `pass` |
   | `accept`, `reject`, `not_run` | `fail` |
   | `not_run` only | `None` |
   | `flag`, `not_run` | `pass` |
   | `accept`, `not_run`, `maybe` | `None` |

   Also, in `apps/web-server/tests/test_pr_verify_evidence.py`:
   - `_votes_text({"splits": {"3-0": 2}, "not_run_excluded": 1})` ends with
     `; 1 not run (excluded)`;
   - with no splits it reads `not reported; 1 not run (excluded)`.

   And a gate test that runs the real `tfactory_verdict` against a stubbed TFactory response
   (`accept` + `not_run`), so the verdict is `pass` and the vote dict carries
   `not_run_excluded == 1`. It goes in the backend test file, using the existing `_run` helper.

   **Verify:**
   ```bash
   PYTHONPATH=apps/backend /mnt/code/Source-home/GitHub/AIFactory/apps/backend/.venv/bin/pytest -q apps/backend/pfactory/test_tfactory_verdict.py apps/backend/merge/test_merge_policy.py
   ```
   Then run the web-server merge-gate tests:
   ```bash
   cd apps/web-server && PYTHONPATH=../backend <W>/pytest -q -o asyncio_mode=auto tests/test_pr_endgame_merge_gate.py tests/test_pr_verify_evidence.py tests/test_merge_decision_reaches_the_merge.py tests/test_trusted_contract_merge.py
   ```
   Then run ruff, and after the commit, the `cq_ratchet` ruff and mypy ratchets.

## Tests

- The verify commands above.
- CI green.
- Live: a P1 run whose TFactory run has a `not_run` row reaches a verdict.

## Rollback

Revert the commit. A `not_run` row holds the gate again.

## Review (plan draft, 2026-11-27)

Codex (gpt-6.2-sol) reviewed the draft read-only, and the worktree was unchanged. This is the
plan review the team policy requires.

| Finding | Verdict | Change |
|---|---|---|
| Adopted PRs advance on the same partial evidence (medium) | confirmed (`pr_endgame.py` `verdict_only`) | kept deliberately and stated in the decisions |
| The evidence block hides skipped rows (low) | confirmed | `not_run_excluded` count is shown in the Votes line |
| Test gaps: the `maybe` mix, and the real parser at the boundary (low) | confirmed | tests added |
| Other legitimate verdict values | refuted: TFactory producers emit `accept`, `flag`, `reject` and `not_run` only | none |
