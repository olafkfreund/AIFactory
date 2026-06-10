"""Tests for the PR endgame orchestrator (#71 Phase 4).

The endgame opens a PR, requests a Copilot review, and — only on APPROVED and
only when AIFACTORY_AUTO_MERGE is on — merges and re-tests. CHANGES_REQUESTED, a
timeout, or both flags off must NEVER merge. Every git/gh call is faked via an
injectable runner, so these tests touch no network/git.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

_WS = Path(__file__).resolve().parents[1]
if str(_WS) not in sys.path:
    sys.path.insert(0, str(_WS))

from server.services import pr_endgame as pe  # noqa: E402
from server.services.pr_endgame import CmdResult  # noqa: E402


class FakeRunner:
    """Routes argv → CmdResult by matching substrings; records calls."""

    def __init__(self, routes: dict[str, CmdResult]):
        self.routes = routes
        self.calls: list[list[str]] = []

    def __call__(self, argv, cwd=None):
        self.calls.append(argv)
        joined = " ".join(argv)
        for needle, result in self.routes.items():
            if needle in joined:
                return result
        return CmdResult(0, "", "")

    def saw(self, needle: str) -> bool:
        return any(needle in " ".join(c) for c in self.calls)


# ── flag gates ─────────────────────────────────────────────────────────────


def test_flags_default_off(monkeypatch):
    monkeypatch.delenv("AIFACTORY_AUTO_PR", raising=False)
    monkeypatch.delenv("AIFACTORY_AUTO_MERGE", raising=False)
    assert pe.is_auto_pr_enabled() is False
    assert pe.is_auto_merge_enabled() is False


def test_flags_on(monkeypatch):
    monkeypatch.setenv("AIFACTORY_AUTO_PR", "true")
    monkeypatch.setenv("AIFACTORY_AUTO_MERGE", "1")
    assert pe.is_auto_pr_enabled() is True
    assert pe.is_auto_merge_enabled() is True


# ── primitives ───────────────────────────────────────────────────────────────


def test_parse_pr_number():
    assert pe._parse_pr_number("https://github.com/o/r/pull/42") == 42
    assert pe._parse_pr_number("no url here") is None


def test_create_pr_parses_number():
    r = FakeRunner({
        "git push": CmdResult(0, "", ""),
        "pr create": CmdResult(0, "https://github.com/o/r/pull/7", ""),
    })
    pr = pe.create_pr(worktree=Path("/tmp"), branch="b", base="main",
                      title="t", body="b", runner=r)
    assert pr == 7
    assert r.saw("pr create")
    # Must configure git auth before pushing, or the deployed push 401s.
    assert r.saw("auth setup-git")


@pytest.mark.parametrize("states,expected", [
    (["APPROVED"], "approved"),
    (["APPROVED", "CHANGES_REQUESTED"], "changes_requested"),  # CR dominates
    (["COMMENTED"], "pending"),
    ([], "pending"),
])
def test_read_review_verdict(states, expected):
    r = FakeRunner({"pulls": CmdResult(0, json.dumps(states), "")})
    assert pe.read_review_verdict("o", "r", 1, runner=r) == expected


# ── watch_and_finish ─────────────────────────────────────────────────────────


def test_changes_requested_never_merges(monkeypatch):
    monkeypatch.setenv("AIFACTORY_AUTO_MERGE", "true")
    r = FakeRunner({"reviews": CmdResult(0, json.dumps(["CHANGES_REQUESTED"]), "")})
    res = asyncio.run(pe.watch_and_finish(owner="o", repo="r", pr=3, runner=r,
                                          poll_interval=0, max_minutes=1))
    assert res["merged"] is False and res["reason"] == "changes_requested"
    assert not r.saw("pr merge")


def test_approved_with_flag_merges_and_retests(monkeypatch):
    monkeypatch.setenv("AIFACTORY_AUTO_MERGE", "true")
    r = FakeRunner({
        "reviews": CmdResult(0, json.dumps(["APPROVED"]), ""),
        "pr merge": CmdResult(0, "merged", ""),
    })
    retested = {"called": False}
    res = asyncio.run(pe.watch_and_finish(
        owner="o", repo="r", pr=5, runner=r, poll_interval=0, max_minutes=1,
        on_approved_merged=lambda: retested.update(called=True),
    ))
    assert res["merged"] is True and res["verdict"] == "approved"
    assert r.saw("pr merge")
    assert retested["called"] is True


def test_approved_without_flag_does_not_merge(monkeypatch):
    monkeypatch.delenv("AIFACTORY_AUTO_MERGE", raising=False)
    r = FakeRunner({"reviews": CmdResult(0, json.dumps(["APPROVED"]), "")})
    res = asyncio.run(pe.watch_and_finish(owner="o", repo="r", pr=5, runner=r,
                                          poll_interval=0, max_minutes=1))
    assert res["merged"] is False and res["reason"] == "auto_merge_disabled"
    assert not r.saw("pr merge")


def test_timeout_hands_to_human(monkeypatch):
    monkeypatch.setenv("AIFACTORY_AUTO_MERGE", "true")
    r = FakeRunner({"reviews": CmdResult(0, json.dumps(["COMMENTED"]), "")})
    res = asyncio.run(pe.watch_and_finish(owner="o", repo="r", pr=9, runner=r,
                                          poll_interval=0, max_minutes=1))
    assert res["merged"] is False and res["reason"] == "review_timeout"


# ── full chain (inline) ──────────────────────────────────────────────────────


def test_run_pr_endgame_full_chain(monkeypatch):
    monkeypatch.setenv("AIFACTORY_AUTO_MERGE", "true")
    r = FakeRunner({
        "git push": CmdResult(0, "", ""),
        "pr create": CmdResult(0, "https://github.com/o/r/pull/11", ""),
        "requested_reviewers": CmdResult(0, "", ""),
        "reviews": CmdResult(0, json.dumps(["APPROVED"]), ""),
        "pr merge": CmdResult(0, "merged", ""),
    })
    res = asyncio.run(pe.run_pr_endgame(
        spec_dir=Path("/tmp"), spec_id="010-x", worktree=Path("/tmp"),
        branch="auto-claude/010-x", base="main", repo="o/r",
        runner=r, background=False,
    ))
    assert res["ok"] and res["pr"] == 11 and res["merged"] is True
    assert r.saw("requested_reviewers")  # Copilot review was requested


def test_run_pr_endgame_no_repo():
    res = asyncio.run(pe.run_pr_endgame(
        spec_dir=Path("/tmp"), spec_id="x", worktree=Path("/tmp"),
        branch="b", base="main", repo="", background=False,
    ))
    assert res["ok"] is False and res["reason"] == "no_repo"


# ── gather_pr_context ────────────────────────────────────────────────────────


def test_gather_pr_context_no_worktree(tmp_path):
    assert pe.gather_pr_context(tmp_path, tmp_path, "spec-1") is None


def test_gather_pr_context_resolves_repo(tmp_path):
    spec_id = "spec-1"
    wt = tmp_path / ".aifactory" / "worktrees" / "tasks" / spec_id
    wt.mkdir(parents=True)
    spec_dir = tmp_path / ".aifactory" / "specs" / spec_id
    spec_dir.mkdir(parents=True)
    (spec_dir / "requirements.json").write_text(json.dumps({"github_repo": "olafkfreund/demo"}))
    r = FakeRunner({"rev-parse": CmdResult(0, "auto-claude/spec-1", "")})
    ctx = pe.gather_pr_context(tmp_path, spec_dir, spec_id, runner=r)
    assert ctx is not None
    assert ctx["branch"] == "auto-claude/spec-1"
    assert ctx["repo"] == "olafkfreund/demo"
