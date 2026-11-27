---
status: draft
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
