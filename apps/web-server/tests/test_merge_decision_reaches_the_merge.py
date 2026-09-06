"""The merge must FLOW THROUGH the decision matrix, not merely agree with it (#1485).

``merge.merge_policy.decide_merge`` -- the RFC-0011/RFC-0013 matrix enforcing the
TFactory verdict, the RFC-0006 VAL floor and RFC-0009 CI parity -- spent months
exported, unit-tested, green and *unreachable*: the real merge path asked only
``tier_permits_auto_merge``. #1479 wired it up; this file is about making that
state unreachable again.

Why the existing suites cannot do it. Revert only ``pr_endgame.py`` and keep the
policy fix and ``tests/test_merge_gate_cluster.py`` still passes 19/19: every
test there calls ``decide_merge`` itself, so it measures the function, never the
path to it. ``test_pr_endgame_merge_gate.py`` does fail that mutation, but it
asserts OUTCOMES (a handback withholds the merge). An outcome test agrees with a
second, independent copy of the rule just as happily as with the matrix -- and a
reimplemented rule is exactly how this gate died the first time.

So these assert the PATH, on the real entry point, and they are indifferent to
what the matrix decides:

  * ``decide_merge`` is consulted, and consulted BEFORE any ``gh pr merge`` is
    attempted -- the merge primitive itself, reached through the real
    ``watch_and_finish``, not a stub;
  * its answer is LOAD-BEARING: a stubbed ``hold-async`` stops the merge even
    with every other signal green, so a caller cannot consult the matrix and
    then ignore it.

Both are paired with a control that merges, because a guard asserting "no merge
happened" passes just as well when the run never got near a merge -- and that is
the pass-shaped empty measurement this issue is about.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

_WS = Path(__file__).resolve().parents[1]
_BACKEND = _WS.parents[0] / "backend"
for _p in (str(_WS), str(_BACKEND)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from merge import merge_policy  # noqa: E402
from server.services import pr_endgame as pe  # noqa: E402
from server.services.pr_endgame import CmdResult, ReviewState  # noqa: E402

_DECIDE = "decide_merge"
_MERGE = "gh pr merge"

_UNSET = object()


class _Runner:
    """Every gh/git call succeeds; the merge attempt is recorded in *log*."""

    def __init__(self, log: list[str]) -> None:
        self.log = log

    def __call__(self, argv: list[str], _cwd: str | None = None) -> CmdResult:
        if argv[:3] == ["gh", "pr", "merge"]:
            self.log.append(_MERGE)
        return CmdResult(0, "https://github.com/owner/repo/pull/42", "")


async def _drive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    decision: object = _UNSET,
) -> list[str]:
    """Run the endgame from ``run_pr_endgame`` through to the merge primitive.

    Returns the ordered log of the two events that matter: the matrix being
    consulted, and a merge being attempted. ``decision``, when given, replaces
    what the matrix answers -- the point being that the PATH is asserted, not
    the answer.
    """
    log: list[str] = []
    real_decide = merge_policy.decide_merge

    def _spy(tier: str, **kwargs: object) -> str:
        log.append(_DECIDE)
        if decision is _UNSET:
            return str(real_decide(tier, **kwargs))
        return str(decision)

    # pr_endgame imports decide_merge lazily, inside the call, so patching the
    # attribute on the policy module is what the production path actually reads.
    monkeypatch.setattr(merge_policy, "decide_merge", _spy)

    spec = tmp_path / ".aifactory" / "specs" / "001-x"
    spec.mkdir(parents=True)

    monkeypatch.setattr(pe, "create_pr", lambda **_k: 42)
    monkeypatch.setattr(pe, "request_copilot_review", lambda *_a, **_k: True)
    monkeypatch.setattr(pe, "_pr_title_body", lambda _d, _s: ("t", "b"))
    # A clean Copilot approval: the one verdict that reaches the merge.
    monkeypatch.setattr(
        pe,
        "read_review_verdict",
        lambda *_a, **_k: ReviewState(
            verdict="approved", copilot_reviewed=True, copilot_approved=True
        ),
    )

    # watch_and_finish polls on a 30s interval; nothing here waits on wall time.
    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    await pe.run_pr_endgame(
        spec_dir=spec,
        spec_id="001-x",
        worktree=spec,
        branch="aifactory/001-x",
        base="main",
        repo="owner/repo",
        auto_merge=True,
        review_tier="auto",
        reviewer="copilot",
        runner=_Runner(log),
        background=False,
    )
    return log


async def test_the_matrix_is_consulted_before_any_merge_is_attempted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wiring itself. Disconnect ``decide_merge`` and this goes red even
    though every gate still decides exactly what it decided before."""
    log = await _drive(tmp_path, monkeypatch)
    # Not a vacuous pass: this run really did reach the merge primitive.
    assert _MERGE in log, f"the run never attempted a merge; log={log}"
    assert _DECIDE in log, (
        "the merge path never consulted merge_policy.decide_merge -- the "
        f"decision matrix is disconnected again (#1485); log={log}"
    )
    assert log.index(_DECIDE) < log.index(_MERGE), (
        f"decide_merge was consulted only AFTER the merge; log={log}"
    )


async def test_a_hold_from_the_matrix_stops_the_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Consulted is not enough -- the answer has to be the one that decides.

    Everything else is green (auto tier, no handback, Copilot approved); only
    the matrix says hold. A caller that reimplements the rule, or reads the
    disposition and merges anyway, fails here.
    """
    log = await _drive(tmp_path, monkeypatch, decision=merge_policy.HOLD_ASYNC)
    assert _DECIDE in log
    assert _MERGE not in log, (
        f"merged despite merge_policy answering {merge_policy.HOLD_ASYNC}; log={log}"
    )


async def test_the_same_run_merges_when_the_matrix_says_auto_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control for the test above: identical run, matrix says merge, and it
    merges. Without this, 'no merge happened' would also pass for a run that
    never got near one."""
    log = await _drive(tmp_path, monkeypatch, decision=merge_policy.AUTO_MERGE)
    assert log == [_DECIDE, _MERGE]
