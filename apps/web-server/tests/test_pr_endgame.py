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


COPILOT = "copilot-pull-request-reviewer[bot]"


def _reviews(*pairs):
    """pairs of (state, login) → the JSON gh returns for .../reviews."""
    return CmdResult(0, json.dumps([{"state": s, "login": l} for s, l in pairs]), "")


def test_read_review_verdict_copilot_aware():
    r = FakeRunner({"reviews": _reviews(("APPROVED", COPILOT))})
    rs = pe.read_review_verdict("o", "r", 1, runner=r)
    assert rs.verdict == "approved" and rs.copilot_approved and rs.copilot_reviewed

    r = FakeRunner({"reviews": _reviews(("APPROVED", "alice"))})  # human, not copilot
    rs = pe.read_review_verdict("o", "r", 1, runner=r)
    assert rs.verdict == "approved" and not rs.copilot_approved and not rs.copilot_reviewed

    r = FakeRunner({"reviews": _reviews(("CHANGES_REQUESTED", COPILOT), ("APPROVED", "alice"))})
    rs = pe.read_review_verdict("o", "r", 1, runner=r)
    assert rs.verdict == "changes_requested" and rs.copilot_changes_requested


# ── watch_and_finish (Copilot-gated) ─────────────────────────────────────────


def test_changes_requested_never_merges():
    r = FakeRunner({"reviews": _reviews(("CHANGES_REQUESTED", COPILOT))})
    res = asyncio.run(pe.watch_and_finish(owner="o", repo="r", pr=3, auto_merge=True,
                                          runner=r, poll_interval=0, max_minutes=1))
    assert res["merged"] is False and res["reason"] == "changes_requested"
    assert res["copilot_changes_requested"] is True
    assert not r.saw("pr merge")


def test_copilot_approved_with_flag_merges_and_retests():
    r = FakeRunner({
        "reviews": _reviews(("APPROVED", COPILOT)),
        "pr merge": CmdResult(0, "merged", ""),
    })
    retested = {"called": False}
    res = asyncio.run(pe.watch_and_finish(
        owner="o", repo="r", pr=5, auto_merge=True, runner=r, poll_interval=0, max_minutes=1,
        on_approved_merged=lambda: retested.update(called=True),
    ))
    assert res["merged"] is True and res["verdict"] == "approved"
    assert r.saw("pr merge") and retested["called"] is True


def test_copilot_approved_without_flag_does_not_merge():
    r = FakeRunner({"reviews": _reviews(("APPROVED", COPILOT))})
    res = asyncio.run(pe.watch_and_finish(owner="o", repo="r", pr=5, auto_merge=False,
                                          runner=r, poll_interval=0, max_minutes=1))
    assert res["merged"] is False and res["reason"] == "auto_merge_disabled"
    assert not r.saw("pr merge")


def test_human_approval_alone_does_not_merge_when_require_copilot():
    # The whole point: don't merge around Copilot. A human APPROVED but Copilot
    # hasn't reviewed → keep waiting → timeout → human-stop, never merge.
    r = FakeRunner({"reviews": _reviews(("APPROVED", "alice"))})
    res = asyncio.run(pe.watch_and_finish(owner="o", repo="r", pr=5, auto_merge=True,
                                          runner=r, poll_interval=0, max_minutes=1))
    assert res["merged"] is False and "review_timeout" in res["reason"]
    assert not r.saw("pr merge")


def test_no_copilot_review_times_out_without_merge():
    r = FakeRunner({"reviews": _reviews(("COMMENTED", COPILOT))})
    res = asyncio.run(pe.watch_and_finish(owner="o", repo="r", pr=9, auto_merge=True,
                                          runner=r, poll_interval=0, max_minutes=1))
    assert res["merged"] is False and "review_timeout" in res["reason"]


def test_require_copilot_false_allows_human_approval():
    r = FakeRunner({
        "reviews": _reviews(("APPROVED", "alice")),
        "pr merge": CmdResult(0, "merged", ""),
    })
    res = asyncio.run(pe.watch_and_finish(owner="o", repo="r", pr=5, auto_merge=True,
                                          require_copilot=False, runner=r,
                                          poll_interval=0, max_minutes=1))
    assert res["merged"] is True


# ── per-project settings flags ───────────────────────────────────────────────


def test_flags_read_project_env_over_global(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFACTORY_AUTO_PR", "false")  # global default off
    env_dir = tmp_path / ".aifactory"
    env_dir.mkdir()
    (env_dir / ".env").write_text("AIFACTORY_AUTO_PR=true\nAIFACTORY_AUTO_MERGE=false\n")
    # project setting (true) overrides the global env (false)
    assert pe.is_auto_pr_enabled(tmp_path) is True
    assert pe.is_auto_merge_enabled(tmp_path) is False
    # no project_path → falls back to global env
    assert pe.is_auto_pr_enabled(None) is False


# ── full chain (inline) ──────────────────────────────────────────────────────


def test_run_pr_endgame_full_chain():
    r = FakeRunner({
        "git push": CmdResult(0, "", ""),
        "pr create": CmdResult(0, "https://github.com/o/r/pull/11", ""),
        "requested_reviewers": CmdResult(0, "", ""),
        "reviews": _reviews(("APPROVED", COPILOT)),  # Copilot approved → gate satisfied
        "pr merge": CmdResult(0, "merged", ""),
    })
    res = asyncio.run(pe.run_pr_endgame(
        spec_dir=Path("/tmp"), spec_id="010-x", worktree=Path("/tmp"),
        branch="auto-claude/010-x", base="main", repo="o/r",
        auto_merge=True, reviewer="copilot", runner=r, background=False,
    ))
    assert res["ok"] and res["pr"] == 11 and res["merged"] is True
    assert r.saw("requested_reviewers")  # Copilot review was requested


def test_run_pr_endgame_aifactory_reviewer_uses_engine_verdict():
    # Default reviewer (aifactory): no Copilot request; merge gated on review_fn.
    r = FakeRunner({
        "git push": CmdResult(0, "", ""),
        "pr create": CmdResult(0, "https://github.com/o/r/pull/12", ""),
        "pr merge": CmdResult(0, "merged", ""),
    })
    opened = {}
    res = asyncio.run(pe.run_pr_endgame(
        spec_dir=Path("/tmp"), spec_id="012-x", worktree=Path("/tmp"),
        branch="aifactory/012-x", base="main", repo="o/r", auto_merge=True,
        review_fn=lambda: pe.ReviewState("approved"),
        on_pr_opened=lambda prn: opened.update(pr=prn),
        runner=r, background=False,
    ))
    assert res["ok"] and res["merged"] is True
    assert opened.get("pr") == 12           # reviewer trigger fired with the PR number
    assert not r.saw("requested_reviewers")  # Copilot NOT requested


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


# ── #71 Phase A: configurable reviewer + AIFactory-verdict gate ──────────────


def test_resolve_pr_reviewer(tmp_path, monkeypatch):
    monkeypatch.delenv("AIFACTORY_PR_REVIEWER", raising=False)
    assert pe.resolve_pr_reviewer(None) == "aifactory"  # default
    monkeypatch.setenv("AIFACTORY_PR_REVIEWER", "copilot")
    assert pe.resolve_pr_reviewer(None) == "copilot"
    monkeypatch.setenv("AIFACTORY_PR_REVIEWER", "bogus")
    assert pe.resolve_pr_reviewer(None) == "aifactory"  # invalid → default
    # project .env wins
    d = tmp_path / ".aifactory"; d.mkdir()
    (d / ".env").write_text("AIFACTORY_PR_REVIEWER=any\n")
    assert pe.resolve_pr_reviewer(tmp_path) == "any"


def test_verdict_from_review_result():
    assert pe.verdict_from_review_result(None).verdict == "pending"
    assert pe.verdict_from_review_result({}).verdict == "pending"
    # AIFactory engine's MergeVerdict vocabulary
    assert pe.verdict_from_review_result({"verdict": "ready_to_merge"}).verdict == "approved"
    assert pe.verdict_from_review_result({"verdict": "needs_revision"}).verdict == "changes_requested"
    assert pe.verdict_from_review_result({"verdict": "blocked"}).verdict == "changes_requested"
    assert pe.verdict_from_review_result({"verdict": "merge_with_changes"}).verdict == "changes_requested"
    # generic synonyms
    assert pe.verdict_from_review_result({"verdict": "approve"}).verdict == "approved"
    assert pe.verdict_from_review_result({"verdict": "request_changes"}).verdict == "changes_requested"
    # blockers field forces changes_requested + carries them as findings
    rs = pe.verdict_from_review_result({"verdict": "ready_to_merge", "blockers": [{"title": "b"}]})
    assert rs.verdict == "changes_requested" and rs.findings == [{"title": "b"}]
    # derive from findings when no verdict
    assert pe.verdict_from_review_result({"findings": []}).verdict == "approved"
    assert pe.verdict_from_review_result({"findings": [{"severity": "high"}]}).verdict == "changes_requested"
    # tolerate {data:{...}} wrapper
    assert pe.verdict_from_review_result({"data": {"verdict": "ready_to_merge"}}).verdict == "approved"


def test_aifactory_reviewer_gate_merges_on_engine_approval():
    # review_fn supplies the AIFactory engine verdict; no GitHub review state read.
    r = FakeRunner({"pr merge": CmdResult(0, "merged", "")})
    res = asyncio.run(pe.watch_and_finish(
        owner="o", repo="r", pr=5, auto_merge=True,
        review_fn=lambda: pe.ReviewState("approved"),
        runner=r, poll_interval=0, max_minutes=1,
    ))
    assert res["merged"] is True
    assert not r.saw("reviews")  # GitHub review state was NOT consulted


def test_aifactory_reviewer_changes_requested_human_stop():
    r = FakeRunner({})
    res = asyncio.run(pe.watch_and_finish(
        owner="o", repo="r", pr=5, auto_merge=True,
        review_fn=lambda: pe.ReviewState("changes_requested"),
        runner=r, poll_interval=0, max_minutes=1,
    ))
    assert res["merged"] is False and res["reason"] == "changes_requested"
    assert not r.saw("pr merge")


# ── #71 Phase B: bounded auto-feedback loop ──────────────────────────────────


def test_fix_loop_fixes_then_merges():
    # review: changes_requested → (fix) → approved → merge.
    states = [pe.ReviewState("changes_requested", findings=[{"severity": "high", "title": "x"}]),
              pe.ReviewState("approved")]
    fixes = []
    def review_fn():
        return states.pop(0) if states else pe.ReviewState("approved")
    def fix_fn(findings):
        fixes.append(findings)
        return True
    r = FakeRunner({"pr merge": CmdResult(0, "merged", "")})
    res = asyncio.run(pe.watch_and_finish(
        owner="o", repo="r", pr=5, auto_merge=True, review_fn=review_fn, fix_fn=fix_fn,
        runner=r, poll_interval=0, max_minutes=5,
    ))
    assert res["merged"] is True
    assert len(fixes) == 1 and fixes[0][0]["title"] == "x"  # findings passed to fixer


def test_fix_loop_bounded_then_human_stop():
    # always changes_requested → exhausts cycles → needs_human_after_fixes.
    def review_fn():
        return pe.ReviewState("changes_requested", findings=[{"severity": "high"}])
    cycles = {"n": 0}
    def fix_fn(findings):
        cycles["n"] += 1
        return True
    r = FakeRunner({})
    res = asyncio.run(pe.watch_and_finish(
        owner="o", repo="r", pr=5, auto_merge=True, review_fn=review_fn, fix_fn=fix_fn,
        max_fix_cycles=2, runner=r, poll_interval=0, max_minutes=5,
    ))
    assert res["merged"] is False and res["reason"] == "needs_human_after_fixes"
    assert cycles["n"] == 2 and res["fix_cycles"] == 2
    assert not r.saw("pr merge")


def test_fix_loop_fix_failure_stops():
    def review_fn():
        return pe.ReviewState("changes_requested", findings=[{"severity": "high"}])
    res = asyncio.run(pe.watch_and_finish(
        owner="o", repo="r", pr=5, auto_merge=True, review_fn=review_fn,
        fix_fn=lambda f: False, runner=FakeRunner({}), poll_interval=0, max_minutes=5,
    ))
    assert res["merged"] is False and res["reason"] == "fix_failed"


def test_no_fix_fn_changes_requested_human_stops():
    # Without a fix_fn, changes_requested is an immediate human-stop (Phase A behavior).
    res = asyncio.run(pe.watch_and_finish(
        owner="o", repo="r", pr=5, auto_merge=True,
        review_fn=lambda: pe.ReviewState("changes_requested"),
        runner=FakeRunner({}), poll_interval=0, max_minutes=1,
    ))
    assert res["reason"] == "changes_requested" and res["fix_cycles"] == 0
