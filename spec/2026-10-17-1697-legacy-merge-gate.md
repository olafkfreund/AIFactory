---
status: approved
issue: 1697
intent: intent/2026-10-17-1697-legacy-merge-gate.md
---

# Spec: legacy and from-issue tasks need a real verified green to auto-merge

## Decisions (user, 2026-10-17)

- **Option B, enforce.** Legacy and from-issue tasks get the #1695
  merge-time gate: auto-merge needs a real TFactory pass and green host CI
  on the head commit.
- `merge_gate_signals` no longer defaults a missing verdict to `pass` or
  missing host CI to green. Missing means "not passed".
- The path floor is always enforced for legacy tasks in `merge_disposition`.
- Accepted consequence: projects without TFactory or CI lose auto-merge for
  these tasks. The CHANGELOG says so.

## Design

All in `apps/web-server/server/services/pr_endgame.py`.

1. **Missing is not passed** (`merge_gate_signals`, `:389-451`). The start
   dict becomes `host_ci_green=False`, `tfactory_verdict=None`. Fetched
   (trusted) values replace them as today. Recorded `task_metadata.json`
   values can only TIGHTEN, always, not just when something was fetched:
   a recorded `pass` verdict or `hostCiGreen: true` is ignored, a recorded
   failure is kept. `task_metadata.json` is written by the build, so it
   is never evidence of a pass. Handback receipt still wins.
2. **Fetch for every task** (`merge_disposition`, `:531-642`). Move the
   `fetch_signals()` call out of the `state == "verified"` branch so a legacy
   task's fetched verdict and CI feed the decision too.
3. **Legacy floor is enforced** (`merge_disposition`). New keyword
   `floor: str | None = None`. When `state == "legacy"`,
   `effective = raise_review_tier(effective, floor)` (from
   `merge.merge_policy`, already used by `apply_path_risk_floor`). Can only
   raise. The floor is the `review_tier_floor` `gather_pr_context` already
   returns (`:1608-1619`) and `run_pr_endgame` already receives; it is
   threaded through `_clears_with_human_approval` and `_build_merge_gate`.
4. **Gate for legacy too** (`_build_merge_gate`, `:1660-1704`). Drop the
   `resolve_contract` lookup and the legacy early return; the gate is built
   for every auto-merge task. Signature loses nothing callers rely on except
   the `None` return.
5. **PR-open pre-check stays prospective** (`run_pr_endgame`, `:1779-1782`).
   At PR-open nothing has run yet, so with missing = not passed the
   pre-check would switch auto-merge off for every task. The pre-check
   passes an explicit `fetch_signals` that returns pass/green. It only
   decides whether to arm the merge watcher; the merge itself goes through
   the gate, which fetches the real signals for the head commit. Signed
   tasks already behaved this way (they had no fetch at PR-open either).

## Alternatives rejected

- **A, accept**: leaves the hole. Not chosen by the user.
- **C, hold all legacy**: ends hands-free MODE 0 `factory:low`. Not chosen.
- **D, server-side tier**: migration plus new table; still trusts a label.
  Not chosen.
- Keeping the pass/green default and only fetching for legacy: the user
  asked that missing mean not passed; any direct caller without a fetch
  would still see a free green.
- Reading the floor back from `pathRiskFloor` in `task_metadata.json`
  instead of threading it: the write is best-effort, so a lost write would
  silently drop the floor. Threading the in-memory value fails closed.

## Risks

- Projects without TFactory or CI: legacy tasks wait, then time out to a
  human stop. Accepted, documented in CHANGELOG.
- Signed tasks: unchanged on the real gate (they already fetched). Direct
  callers of `merge_disposition` without `fetch_signals` now hold; the only
  production one is the pre-check, handled in design 5.
- Tests that call `merge_disposition` directly and expect `auto-merge` from
  metadata `_GREEN` need a `fetch_signals`; tests asserting "legacy never
  fetches" / "no gate for legacy" are inverted.

## Verification

- New regression tests in `apps/web-server/tests/test_pr_endgame_merge_gate.py`:
  a legacy task with no fetched verdict holds; a legacy `low` task with a
  `blocking` floor holds; the gate is built for legacy and returns False
  when TFactory gives no verdict. They fail on `origin/dev` and pass after.
- `apps/backend/.venv/bin/pytest apps/web-server/tests -q -o asyncio_mode=auto`
  green; `ruff check` and `ruff format --check` on the touched files.
