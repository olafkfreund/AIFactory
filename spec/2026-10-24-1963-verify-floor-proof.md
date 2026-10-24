---
status: draft
issue: Factory#1963
intent: intent/2026-10-24-1963-verify-floor-proof.md
---

# Spec: prove a passing verdict cannot merge past a red floor

## Design

Add one parametrised async test to
`apps/web-server/tests/test_pr_endgame_merge_gate.py`. It drives the real
path: `pe.run_pr_endgame(background=False)`, which builds the merge-time gate
(`_build_merge_gate`, `pr_endgame.py:1817-1868`), wraps it in the receipt veto,
and awaits the real `watch_and_finish` (`pr_endgame.py:1400-1631`).

Only the external edges are stubbed, at seams the module already has:

| Seam | Stub |
| ---- | ---- |
| `pe.create_pr` | returns PR `42`; for the metadata cases it also writes `task_metadata.json` (see below) |
| `pe._pr_title_body` | `("t", "b")` |
| `pfactory.tfactory_client.tfactory_verdict` | always `"pass"`, the permissive model verdict |
| `pe.host_ci_green` | `True`, or `False` for the host-CI case |
| `pe.required_checks_green` | `True` |
| `pe._pr_head` | `"deadbeef"` |
| `pe.merge_pr` | spy that records each call and returns `True` |
| `pe.watch_and_finish` | the real function with `poll_interval=0, max_minutes=1` added |
| `review_fn` | `lambda: pe.ReviewState("approved")`, with `reviewer="aifactory"` |

Cases (`ids` in brackets):

1. `[host_ci_red]`: `host_ci_green` returns `False`.
2. `[val_below_floor]`: `task_metadata.json` gets
   `{"achievedVal": "VAL-1", "valFloor": "VAL-3"}`.
3. `[ci_parity_false]`: `task_metadata.json` gets `{"ciParity": false}`.
4. `[all_green]` control: nothing red.

Cases 2 and 3 write the metadata inside the `create_pr` stub, after the
endgame has already decided `auto_merge` at PR-open time. So every red case
reaches the merge-time gate with `auto_merge=True`. This models a measurement
that lands after the PR opens. It proves the floor holds at merge, not only at
open, which the existing tests already cover (lines 139-158).

Assertions:

- Red cases: the `merge_pr` spy has no calls, `result["merged"] is False`,
  and `result["reason"] == "merge_gate_timeout"`. That reason shows the gate
  held. Another early exit would give a different reason.
- Control: `merge_pr` is called exactly once with `match_head="deadbeef"`, and
  `result["merged"] is True`. Without this case the red cases would also pass
  if merging were simply switched off.

Mutation proof (run once by hand, not committed). Remove one conjunct at a time
from `decide_merge` (`apps/backend/merge/merge_policy.py:306-311`):
`host_ci_green` (307), `_val_meets_floor(...)` (309) or `ci_parity` (310).
The matching case must then fail. Revert after each run.

Test only. If a red case merges, that is a bug: fix it in `pr_endgame.py` or
`merge_policy.py` and update `plan/` in the same commit.

## Alternatives rejected

- **Call `_build_merge_gate` and `watch_and_finish` directly.** That skips the
  receipt veto and the `run_pr_endgame` wiring, and it is not end to end.
- **Write the metadata before the PR opens.** The PR-open check would set
  `auto_merge=False`, so `watch_and_finish` returns `auto_merge_disabled` and
  never reaches the merge gate. The existing tests already cover this.
- **Patch `asyncio.sleep` to speed up polling.** This is global, and it also
  hits the receipt veto's loop. Passing `poll_interval=0` is the parameter
  the function already takes.
- **The PFactory slice (verify verdict and votes in the run-signature
  header).** Skipped. `PlanSession` (`PFactory apps/backend/plan/service.py:97`)
  has no verify verdict or vote field. Nothing in PFactory writes one: the
  audit pack's `getattr(session, "tfactory_verdict", None)` is always `None`.
  The header is rendered when the plan's docs are emitted, which is before
  AIFactory builds or TFactory verifies, so the field would always be null. To
  carry it, TFactory would have to call back into PFactory to store the
  verdict, and the docs would have to be rendered again. That is new plumbing
  and needs its own issue.

## Risks

- Test only, so there is no runtime risk.
- About 60 gate polls per red case, each reading two small files. Expected
  well under a second, in line with the file's current 2 s.
- If `pfactory.tfactory_client` stops importing, `fetch` returns a `None`
  verdict and the red cases pass for the wrong reason. The control case
  catches that, because it would no longer merge.

## Verification

- `apps/backend/.venv/bin/pytest apps/web-server/tests/test_pr_endgame_merge_gate.py -q -o asyncio_mode=auto`
  passes: 42 existing tests plus 4 new.
- `ruff check` and `ruff format --check` on the test file are clean.
- The mutation runs above each fail their matching case.
