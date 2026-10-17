---
status: approved
issue: 1697
spec: spec/2026-10-17-1697-legacy-merge-gate.md
---

# Plan: legacy and from-issue tasks need a real verified green to auto-merge

Approved decisions (user, 2026-10-17, option B):

- Legacy (no signed contract) and from-issue tasks get the #1695 merge-time
  gate: auto-merge needs a real TFactory pass and green host CI on the head.
- `merge_gate_signals`: missing verdict / missing host CI = not passed.
  `task_metadata.json` values may only tighten, never loosen.
- `merge_disposition` always enforces the path floor for legacy tasks.
- The PR-open pre-check passes an explicit prospective pass/green fetch; the
  real merge goes through the gate, which fetches for the head commit.
- Projects without TFactory or CI lose auto-merge for these tasks; CHANGELOG
  says so.

File for steps 1: `apps/web-server/server/services/pr_endgame.py` (PE).

## Steps

1. PE, five edits:
   a. `merge_gate_signals` (`:389-451`): start dict
      `host_ci_green=False`, `tfactory_verdict=None`. Import `_verdict_pass`
      unconditionally (lazy, inside the function as today). In the metadata
      loop, skip a recorded passing verdict always (drop the
      `fetched is not None and` part), and skip a recorded `host_ci_green`
      that is True always. Rewrite the docstring's "NOT MEASURED" paragraph:
      unmeasured now means not passed.
   b. `merge_disposition` (`:531-642`): add keyword `floor: str | None = None`.
      Move `if fetch_signals is not None: fetched = fetch_signals()` out of
      the `verified` branch so it runs for any non-hold, readable state
      (after the `unreadable` check is fine, but keep it inside the `try`).
      For `state == "legacy"`: `effective = raise_review_tier(effective, floor)`
      (import `raise_review_tier` with `decide_merge` from
      `merge.merge_policy`). Update the docstring.
   c. `_clears_with_human_approval` (`:1035-1055`): add keyword
      `floor: str | None = None`, pass it to `merge_disposition`.
   d. `_build_merge_gate` (`:1655-1704`): add parameter `review_tier_floor`
      after `review_tier`; drop the `resolve_contract` lookup and the legacy
      `return None`; return type `Callable[[str], bool]`; pass the floor to
      both `merge_disposition` and `_clears_with_human_approval`. Docstring:
      "The merge-time gate for every auto-merge task".
   e. `run_pr_endgame` (`:1779-1817`): define
      `def _prospective() -> dict[str, object]: return {"tfactory_verdict": "pass", "host_ci_green": True}`
      with a one-line why comment; pass `fetch_signals=_prospective` and
      `floor=review_tier_floor` to the pre-check `merge_disposition` and
      `_clears_with_human_approval`; pass `review_tier_floor` to
      `_build_merge_gate`.
   → verify by `ruff check` + `ruff format --check` on PE.
   Traps: never log raw tier values (use `_describe_tier` + `sanitize_log`);
   keep `# noqa` comments; no reformatting of untouched code.

2. Tests, `apps/web-server/tests/test_pr_endgame_merge_gate.py` and
   `apps/web-server/tests/test_trusted_contract_merge.py`:
   - Update direct `merge_disposition` calls that expect `auto-merge` from
     metadata greens to pass `fetch_signals=lambda: {"tfactory_verdict":
     "pass", "host_ci_green": True}`.
   - `test_1695_no_fetch_is_todays_dict`: expect `host_ci_green False`,
     `tfactory_verdict None`.
   - Invert `test_1695_legacy_never_calls_the_fetch` (legacy now calls it)
     and `test_1695_endgame_arms_the_gate_only_for_non_legacy` (gate armed
     for legacy too).
   - Add regression tests (fail on origin/dev, pass after):
     `test_1697_legacy_without_fetched_pass_holds` (legacy `auto`,
     fetch returns verdict None -> not auto-merge),
     `test_1697_legacy_floor_is_enforced` (legacy `low`, `floor="blocking"`,
     fetched pass -> `hold-blocking`),
     `test_1697_metadata_pass_does_not_loosen` (no fetch, metadata
     `_GREEN` -> verdict None, host CI False).
   → verify by `apps/backend/.venv/bin/pytest apps/web-server/tests -q -o asyncio_mode=auto`.
   Traps: tests run from repo root with `-o asyncio_mode=auto`; conftest
   stubs `on_record`; `_isolated_host` autouse fixture needed for isolation.

3. `CHANGELOG.md` `[Unreleased]` / `### Security`: one entry for #1697,
   naming the accepted consequence (no TFactory or no CI = no auto-merge for
   legacy/from-issue tasks). → verify by reading it.
   Traps: none.

## Tests

`apps/backend/.venv/bin/pytest apps/web-server/tests -q -o asyncio_mode=auto`
all green; `ruff check` and `ruff format --check` on touched Python files.
Regression tests proven failing against origin/dev's PE.

## Rollback

Revert the PR. No data, schema or config change.
