"""Slice 2 of #543: watch_and_finish runs a bounded conflict-resolution +
re-review loop when an approved PR won't merge.

Async tests with a sequencing fake runner (a marker can yield different results
across polls) + a scripted review_fn. poll_interval=0 keeps them fast.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "web-server"))

from server.services.pr_endgame import (  # noqa: E402
    CmdResult,
    ReviewState,
    watch_and_finish,
)

_OK = CmdResult(0, "", "")
_CONFLICT = CmdResult(1, "", "merge conflict")  # gh blob contains "conflict"
_FAIL = CmdResult(1, "", "boom")


class SeqRunner:
    """Scripted runner. ``script`` maps a marker (substring of joined argv) to a
    LIST of CmdResults consumed in order (last repeats once exhausted). Unmatched
    commands succeed. Records calls."""

    def __init__(self, script: dict[str, list[CmdResult]]):
        self.script = {k: list(v) for k, v in script.items()}
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], cwd: str | None = None) -> CmdResult:
        self.calls.append(argv)
        joined = " ".join(argv)
        for marker, results in self.script.items():
            if marker in joined:
                return results.pop(0) if len(results) > 1 else results[0]
        return _OK

    def ran(self, marker: str) -> bool:
        return any(marker in " ".join(c) for c in self.calls)


def _approved() -> ReviewState:
    return ReviewState(verdict="approved", copilot_approved=True)


@pytest.mark.asyncio
async def test_conflict_resolved_then_remerges_after_rereview():
    # merge: fails (conflict) on poll 1, succeeds on poll 2 after the resolve+push.
    runner = SeqRunner(
        {
            "gh pr merge": [_CONFLICT, _OK],
            "gh pr update-branch": [_FAIL],  # true conflict -> merge_pr returns False
            "git rebase origin/main": [_FAIL],  # conflict during resolve
            "diff --name-only --diff-filter=U": [CmdResult(0, "app.py", "")],
            "rebase --continue": [_OK],
            "git push --force-with-lease": [_OK],
        }
    )
    fixed = []
    out = await watch_and_finish(
        owner="o",
        repo="r",
        pr=7,
        auto_merge=True,
        review_fn=_approved,
        conflict_fixer=lambda files, wt: fixed.append(files) or True,
        worktree="/wt",
        base_branch="main",
        runner=runner,
        poll_interval=0,
        max_minutes=1,
    )
    assert out["merged"] is True
    assert fixed == [["app.py"]]  # fixer ran once on the conflicted file
    assert runner.ran("git push --force-with-lease")


@pytest.mark.asyncio
async def test_unresolvable_conflict_human_stops():
    runner = SeqRunner(
        {
            "gh pr merge": [_CONFLICT],
            "gh pr update-branch": [_FAIL],
            "git rebase origin/main": [_FAIL],
            "diff --name-only --diff-filter=U": [CmdResult(0, "app.py", "")],
        }
    )
    out = await watch_and_finish(
        owner="o",
        repo="r",
        pr=7,
        auto_merge=True,
        review_fn=_approved,
        conflict_fixer=lambda files, wt: False,  # cannot resolve
        worktree="/wt",
        base_branch="main",
        runner=runner,
        poll_interval=0,
        max_minutes=1,
    )
    assert out["merged"] is False
    assert out["reason"] == "merge_conflict_unresolved"
    assert out["conflict_cycles"] == 1
    assert runner.ran("rebase --abort")


@pytest.mark.asyncio
async def test_no_conflict_fixer_keeps_legacy_merge_failed():
    # Without a conflict_fixer, behaviour is unchanged: approved + merge fails
    # -> merge_failed human-stop (no resolution attempt).
    runner = SeqRunner({"gh pr merge": [_CONFLICT], "gh pr update-branch": [_FAIL]})
    out = await watch_and_finish(
        owner="o",
        repo="r",
        pr=7,
        auto_merge=True,
        review_fn=_approved,
        runner=runner,
        poll_interval=0,
        max_minutes=1,
    )
    assert out["merged"] is False
    assert out["reason"] == "merge_failed"
    assert not runner.ran("git rebase origin/main")  # no resolution attempted
