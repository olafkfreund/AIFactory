"""D1 independent-review gate tests across qa_gate, merger, completion and manual PR (#2233)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi.responses import JSONResponse

_WS = Path(__file__).resolve().parents[1]
if str(_WS) not in sys.path:
    sys.path.insert(0, str(_WS))
_BACKEND = _WS.parent / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from server.project_registry import save_projects  # noqa: E402
from server.routes import pr as pr_routes  # noqa: E402
from server.services import (  # noqa: E402
    completion_orchestration as orch,
    merger as mg,
    pr_endgame as pe,
    qa_gate,
)
from server.services.pr_endgame import CmdResult  # noqa: E402
from server.services.task_control import read_control, write_control  # noqa: E402

_LOG = logging.getLogger(__name__)


class FakeRunner:
    def __init__(self, routes: dict[str, CmdResult]) -> None:
        self.routes = routes
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], _cwd: str | None = None) -> CmdResult:
        self.calls.append(argv)
        joined = " ".join(argv)
        matches = [
            (needle, result)
            for needle, result in self.routes.items()
            if needle in joined
        ]
        if matches:
            return max(matches, key=lambda pair: len(pair[0]))[1]
        return CmdResult(0, "", "")

    def saw(self, needle: str) -> bool:
        return any(needle in " ".join(c) for c in self.calls)


def _write_gate_files(  # noqa: PLR0913 - fixture controls each gate input
    spec_dir: Path,
    *,
    status: str = "approved",
    reviewer_model: str = "opus",
    reviewer_served: str | None = None,
    head_sha: str = "head-sha-1",
    report: bytes = b"# QA Report\nClean\n",
    report_hash: str | None = None,
    identities: list[dict[str, Any]] | None = None,
) -> None:
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "qa_evidence_report.md").write_bytes(report)
    (spec_dir / "qa_evidence.json").write_text(
        json.dumps(
            {
                "status": status,
                "reviewer": {
                    "model": reviewer_model,
                    "provider": "claude",
                    "served_model": reviewer_served,
                },
                "reviewed_head": head_sha,
                "report_sha256": (
                    report_hash
                    if report_hash is not None
                    else hashlib.sha256(report).hexdigest()
                ),
                "qa_cycle": 1,
                "override": None,
                "at": "2026-11-25T00:00:00+00:00",
            }
        )
    )
    if identities is None:
        identities = [
            {
                "phase": "coding",
                "subtask_id": "1.1",
                "model": "sonnet",
                "provider": "claude",
                "served_model": None,
            }
        ]
    (spec_dir / "model_identities.json").write_text(json.dumps(identities))


# ── qa_gate.check ───────────────────────────────────────────────────────────


def test_qa_gate_pass_returns_none(tmp_path: Path) -> None:
    _write_gate_files(tmp_path, head_sha="abc1234")
    assert qa_gate.check(tmp_path, "abc1234") is None


def test_qa_gate_ignores_planning_identity(tmp_path: Path) -> None:
    _write_gate_files(
        tmp_path,
        reviewer_model="opus",
        identities=[
            {"phase": "planning", "model": "opus"},
            {"phase": "coding", "model": "sonnet"},
        ],
    )
    assert qa_gate.check(tmp_path, "head-sha-1") is None


@pytest.mark.asyncio
async def test_auto_merge_rechecks_qa_after_pr_head_moves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_gate_files(tmp_path, head_sha="reviewed-head")
    gate_calls: list[str] = []

    def merge_gate(head: str) -> bool:
        gate_calls.append(head)
        return True

    monkeypatch.setattr(pe, "_pr_head", lambda *_args: "pushed-head")
    merge = Mock()
    monkeypatch.setattr(pe, "merge_pr", merge)
    assert qa_gate.check(tmp_path, "reviewed-head") is None
    result = await pe.watch_and_finish(
        owner="o",
        repo="r",
        pr=44,
        auto_merge=True,
        review_fn=lambda: pe.ReviewState("approved"),
        merge_gate=merge_gate,
        spec_dir=tmp_path,
        poll_interval=0,
        max_minutes=1,
    )
    assert result["reason"] == "merge_gate_timeout"
    merge.assert_not_called()
    assert gate_calls == []


def test_qa_gate_missing_malformed_or_unapproved(tmp_path: Path) -> None:
    # Missing files
    assert qa_gate.check(tmp_path, "abc1234") == "qa_missing"

    # Malformed JSON
    (tmp_path / "qa_evidence.json").write_text("{not-json")
    assert qa_gate.check(tmp_path, "abc1234") == "qa_missing"

    # Status not approved
    for st in ("rejected", "unavailable", "error"):
        _write_gate_files(tmp_path, status=st, head_sha="abc1234")
        assert qa_gate.check(tmp_path, "abc1234") == "qa_missing"


def test_qa_gate_stale_on_head_mismatch_or_changed_report(tmp_path: Path) -> None:
    _write_gate_files(tmp_path, head_sha="abc1234")
    assert qa_gate.check(tmp_path, "other-head") == "qa_stale"
    assert qa_gate.check(tmp_path, None) == "qa_stale"

    # Tampered / changed report copy
    (tmp_path / "qa_evidence_report.md").write_text("edited after verdict")
    assert qa_gate.check(tmp_path, "abc1234") == "qa_stale"

    # Missing protected report copy
    (tmp_path / "qa_evidence_report.md").unlink()
    assert qa_gate.check(tmp_path, "abc1234") == "qa_stale"


def test_qa_gate_not_independent_and_alias_equality(tmp_path: Path) -> None:
    # Exact match
    _write_gate_files(
        tmp_path,
        reviewer_model="sonnet",
        identities=[
            {
                "phase": "coding",
                "subtask_id": "1",
                "model": "sonnet",
                "provider": "claude",
                "served_model": None,
            }
        ],
    )
    assert qa_gate.check(tmp_path, "head-sha-1") == "qa_not_independent"

    # Alias equality: shorthand vs canonical ID
    _write_gate_files(
        tmp_path,
        reviewer_model="claude-sonnet-5-5",
        identities=[
            {
                "phase": "coding",
                "subtask_id": "1",
                "model": "sonnet",
                "provider": "claude",
                "served_model": None,
            }
        ],
    )
    assert qa_gate.check(tmp_path, "head-sha-1") == "qa_not_independent"

    # Equal to qa_fixer identity
    _write_gate_files(
        tmp_path,
        reviewer_model="opus",
        identities=[
            {
                "phase": "coding",
                "subtask_id": "1",
                "model": "sonnet",
                "provider": "claude",
                "served_model": None,
            },
            {
                "phase": "qa_fixer",
                "subtask_id": None,
                "model": "claude-opus-5-5",
                "provider": "claude",
                "served_model": None,
            },
        ],
    )
    assert qa_gate.check(tmp_path, "head-sha-1") == "qa_not_independent"

    # Unknown identity model or empty identities list
    _write_gate_files(
        tmp_path,
        reviewer_model="opus",
        identities=[
            {
                "phase": "coding",
                "subtask_id": "1",
                "model": "",
                "provider": "claude",
                "served_model": None,
            }
        ],
    )
    assert qa_gate.check(tmp_path, "head-sha-1") == "qa_not_independent"

    _write_gate_files(tmp_path, reviewer_model="opus", identities=[])
    assert qa_gate.check(tmp_path, "head-sha-1") == "qa_not_independent"


# ── merger ──────────────────────────────────────────────────────────────────


def _merger_spec(tmp_path: Path, spec_id: str = "001-x") -> Path:
    wt = tmp_path / ".aifactory" / "worktrees" / "tasks" / spec_id
    wt.mkdir(parents=True, exist_ok=True)
    spec_dir = tmp_path / ".aifactory" / "specs" / spec_id
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "requirements.json").write_text(
        json.dumps({"github_repo": "o/r", "title": f"Task {spec_id}"})
    )
    return spec_dir


def _merger_routes(**overrides: CmdResult) -> dict[str, CmdResult]:
    base = {
        "rev-parse --abbrev-ref HEAD": CmdResult(0, "aifactory/001-x", ""),
        "rev-parse refs/heads/aifactory/001-x": CmdResult(0, "head-sha-1", ""),
        "pr list": CmdResult(0, "[]", ""),
        "fetch origin": CmdResult(0, "", ""),
        "rev-list --count": CmdResult(0, "2\n", ""),
        "diff --no-ext-diff --no-textconv --name-only": CmdResult(0, "a.py\n", ""),
        "git push": CmdResult(0, "", ""),
        "pr create": CmdResult(0, "https://github.com/o/r/pull/5", ""),
    }
    base.update(overrides)
    return base


def test_merger_refuses_opened_and_already_open_when_gate_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AIFACTORY_AUTO_PR", "true")
    spec_dir = _merger_spec(tmp_path)
    write_control(spec_dir, status="human_review", review_reason="awaiting_merge")

    # No evidence -> opened refused before push/create
    r_open = FakeRunner(_merger_routes())
    res_open = mg.process_one("proj", tmp_path, spec_dir, runner=r_open)
    assert res_open["action"] == "skipped"
    assert res_open["reason"] == "qa_missing"
    assert not r_open.saw("git push")
    assert not r_open.saw("pr create")
    assert read_control(spec_dir)["reviewReason"] == "qa_missing"

    # Existing OPEN PR with stale head -> already_open refused
    _write_gate_files(spec_dir, head_sha="reviewed-sha")
    open_pr_json = json.dumps(
        [
            {
                "number": 12,
                "state": "OPEN",
                "createdAt": "2026-11-25",
                "headRefOid": "pr-actual-sha",
            }
        ]
    )
    r_existing = FakeRunner(
        _merger_routes(**{"pr list": CmdResult(0, open_pr_json, "")})
    )
    res_existing = mg.process_one("proj", tmp_path, spec_dir, runner=r_existing)
    assert res_existing["action"] == "skipped"
    assert res_existing["reason"] == "qa_stale"
    assert read_control(spec_dir)["reviewReason"] == "qa_stale"


def test_merger_does_not_block_merged_or_closed_prs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AIFACTORY_AUTO_PR", "true")
    spec_dir = _merger_spec(tmp_path)
    # No QA evidence at all
    for state, expected_action, expected_status, expected_reason in (
        ("MERGED", "merged", "done", None),
        ("CLOSED", "closed", "human_review", "pr_closed"),
    ):
        write_control(spec_dir, status="human_review", review_reason="errors")
        pr_json = json.dumps(
            [{"number": 33, "state": state, "createdAt": "2026-11-25"}]
        )
        r = FakeRunner(_merger_routes(**{"pr list": CmdResult(0, pr_json, "")}))
        res = mg.process_one("proj", tmp_path, spec_dir, runner=r)
        assert res["action"] == expected_action
        assert res["pr"] == 33
        ctrl = read_control(spec_dir)
        assert ctrl["status"] == expected_status
        assert ctrl.get("reviewReason") == expected_reason


@pytest.mark.parametrize("reason", ["qa_missing", "qa_stale", "qa_not_independent"])
def test_merger_sync_status_maps_gate_reasons_and_keeps_human_set_status(
    tmp_path: Path, reason: str
) -> None:
    spec_dir = _merger_spec(tmp_path)
    write_control(spec_dir, status="human_review", review_reason="awaiting_merge")
    assert mg._sync_status(spec_dir, {"action": "skipped", "reason": reason}) is True
    ctrl = read_control(spec_dir)
    assert ctrl["status"] == "human_review"
    assert ctrl["reviewReason"] == reason

    # Human-set status outside human_review is never overwritten
    for human_status in ("done", "backlog", "in_progress"):
        write_control(spec_dir, status=human_status, clear_review_reason=True)
        assert (
            mg._sync_status(spec_dir, {"action": "skipped", "reason": reason}) is False
        )
        assert read_control(spec_dir)["status"] == human_status


def test_merger_remote_only_branch_resolves_origin_ref_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AIFACTORY_AUTO_PR", "true")
    spec_dir = _merger_spec(tmp_path)
    _write_gate_files(spec_dir, head_sha="remote-sha-999")
    r = FakeRunner(
        _merger_routes(
            **{
                "rev-parse --verify --quiet refs/heads/aifactory/001-x": CmdResult(
                    1, "", ""
                ),
                "rev-parse --verify --quiet origin/aifactory/001-x": CmdResult(
                    0, "remote-sha-999", ""
                ),
                "rev-parse origin/aifactory/001-x": CmdResult(0, "remote-sha-999", ""),
            }
        )
    )
    res = mg._process_spec("proj", tmp_path, spec_dir, dry_run=False, runner=r)
    assert res["action"] == "opened"
    assert res["pr"] == 5


# ── completion_orchestration ────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("adopted", [False, True])
@pytest.mark.parametrize("unreadable_head", [False, True])
async def test_completion_endgame_marker_skipped_on_failure_and_runs_later(
    tmp_path: Path, adopted: bool, unreadable_head: bool
) -> None:
    spec_id = "025-endgame"
    spec_dir = tmp_path / ".aifactory" / "specs" / spec_id
    spec_dir.mkdir(parents=True)
    (spec_dir / "implementation_plan.json").write_text(
        json.dumps({"phases": [{"name": "build", "subtasks": []}]})
    )
    memory = spec_dir / "memory"
    memory.mkdir()
    (memory / "build_commits.json").write_text(
        json.dumps({"commits": [{"hash": "head-sha-1", "subtask_id": "1.1"}]})
    )

    endgame_mock = AsyncMock(return_value={"ok": True, "pr": 17})
    merger_calls: list[Any] = []

    def fake_merger(*args: Any, **_kwargs: Any) -> dict[str, Any]:
        merger_calls.append(args)
        return {"action": "skipped", "reason": "qa_missing"}

    ctx: dict[str, Any] = {
        "worktree": tmp_path,
        "branch": "aifactory/025-endgame",
        "base": "main",
        "repo": "o/r",
    }
    if adopted:
        ctx["adopted_pr"] = 17

    contract = {"provenance": {"pr_number": 17}} if adopted else None

    with (
        patch("server.services.completion.emit_terminal_completion"),
        patch(
            "pfactory.tfactory_client.maybe_auto_handoff_tfactory",
            new=AsyncMock(return_value={"sent": False}),
        ),
        patch(
            "server.services.completion_orchestration.handoff_contract",
            return_value=contract,
        ),
        patch.object(pe, "is_auto_pr_enabled", return_value=not adopted),
        patch.object(pe, "gather_pr_context", return_value=ctx),
        patch.object(
            pe, "_pr_head", return_value=None if unreadable_head else "head-sha-1"
        ),
        patch("server.services.merger._ref_sha", return_value="head-sha-1"),
        patch.object(pe, "resolve_pr_reviewer", return_value="copilot"),
        patch.object(pe, "run_pr_endgame", endgame_mock),
        patch("server.services.merger.process_one", fake_merger),
    ):
        # First pass: no QA evidence -> gate fails, endgame skipped, no .pr_endgame_done
        await orch.run_terminal_completion(
            spec_dir=spec_dir,
            project_path=tmp_path,
            spec_id=spec_id,
            task_id=f"proj:{spec_id}",
            backend_path=_BACKEND,
            is_terminal=True,
            is_completed=True,
            terminal_status="completed",
            logger=_LOG,
        )
        assert (spec_dir / ".terminal_side_effects_done").exists()
        assert not (spec_dir / ".pr_endgame_done").exists()
        endgame_mock.assert_not_awaited()
        assert len(merger_calls) == 1

        # Second pass after QA evidence arrives -> endgame runs and writes marker
        _write_gate_files(spec_dir, head_sha="head-sha-1")
        await orch.run_terminal_completion(
            spec_dir=spec_dir,
            project_path=tmp_path,
            spec_id=spec_id,
            task_id=f"proj:{spec_id}",
            backend_path=_BACKEND,
            is_terminal=True,
            is_completed=True,
            terminal_status="completed",
            logger=_LOG,
        )
        if adopted and unreadable_head:
            assert not (spec_dir / ".pr_endgame_done").exists()
            endgame_mock.assert_not_awaited()
        else:
            assert (spec_dir / ".pr_endgame_done").exists()
            endgame_mock.assert_awaited_once()
        assert len(merger_calls) == 2


@pytest.mark.asyncio
async def test_overlapping_completion_runs_endgame_once(tmp_path: Path) -> None:
    spec_id = "025-endgame"
    spec_dir = tmp_path / ".aifactory" / "specs" / spec_id
    _write_gate_files(spec_dir)
    (spec_dir / "implementation_plan.json").write_text(
        json.dumps({"phases": [{"name": "build", "subtasks": []}]})
    )
    memory = spec_dir / "memory"
    memory.mkdir()
    (memory / "build_commits.json").write_text(
        json.dumps({"commits": [{"hash": "head-sha-1", "subtask_id": "1.1"}]})
    )
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def endgame(**_kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return {"ok": True}

    async def complete() -> None:
        await orch.run_terminal_completion(
            spec_dir=spec_dir,
            project_path=tmp_path,
            spec_id=spec_id,
            task_id=f"proj:{spec_id}",
            backend_path=_BACKEND,
            is_terminal=True,
            is_completed=True,
            terminal_status="completed",
            logger=_LOG,
        )

    ctx = {"worktree": tmp_path, "branch": "task", "base": "main", "repo": "o/r"}
    with (
        patch("server.services.completion.emit_terminal_completion"),
        patch(
            "pfactory.tfactory_client.maybe_auto_handoff_tfactory",
            new=AsyncMock(return_value={"sent": False}),
        ),
        patch.object(pe, "is_auto_pr_enabled", return_value=True),
        patch.object(pe, "gather_pr_context", return_value=ctx),
        patch("server.services.merger._ref_sha", return_value="head-sha-1"),
        patch.object(pe, "resolve_pr_reviewer", return_value="copilot"),
        patch.object(pe, "run_pr_endgame", side_effect=endgame),
        patch("server.services.merger.process_one", return_value={"action": "skipped"}),
    ):
        first = asyncio.create_task(complete())
        await asyncio.wait_for(entered.wait(), timeout=5)
        await complete()
        release.set()
        await first
    assert calls == 1


# ── manual route: routes/pr.create_pr_from_task ─────────────────────────────


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603
        [  # noqa: S607
            "git",
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _setup_manual_repos(tmp_path: Path, spec_id: str = "001-manual") -> dict[str, Any]:
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare", "--initial-branch", "main")

    project = tmp_path / "project"
    _git(tmp_path, "clone", str(origin), str(project))
    _git(project, "checkout", "-B", "main")
    (project / "README.md").write_text("base\n")
    _git(project, "add", "README.md")
    _git(project, "commit", "-m", "base")
    _git(project, "push", "-u", "origin", "main")

    branch = f"aifactory/{spec_id}"
    worktree = project / ".aifactory" / "worktrees" / "tasks" / spec_id
    worktree.parent.mkdir(parents=True)
    _git(tmp_path, "clone", str(origin), str(worktree))
    _git(worktree, "checkout", "-b", branch)
    (worktree / "feature.py").write_text("x = 1\n")
    _git(worktree, "add", "feature.py")
    _git(worktree, "commit", "-m", "feat")
    head_sha = _git(worktree, "rev-parse", "HEAD")

    spec_dir = project / ".aifactory" / "specs" / spec_id
    spec_dir.mkdir(parents=True)
    (spec_dir / "requirements.json").write_text(
        json.dumps({"title": "Manual PR", "description": "desc"})
    )
    save_projects({"proj": {"path": str(project), "name": "proj"}})
    return {
        "origin": origin,
        "project": project,
        "worktree": worktree,
        "spec_dir": spec_dir,
        "branch": branch,
        "head_sha": head_sha,
        "task_id": f"proj:{spec_id}",
    }


async def _call_manual_create_pr(task_id: str) -> tuple[int, dict[str, Any]]:
    res = await pr_routes.create_pr_from_task(
        task_id, pr_routes.CreatePRFromTaskOptions(), _access={}
    )
    if isinstance(res, JSONResponse):
        return res.status_code, json.loads(bytes(res.body))
    assert isinstance(res, dict)
    return 200, res


@pytest.mark.asyncio
async def test_manual_route_gates_existing_open_pr_but_not_merged(
    tmp_path: Path,
) -> None:
    env = _setup_manual_repos(tmp_path)

    def fake_gh_open(args: list[str], **_kw: Any) -> dict[str, Any]:
        if args[:2] == ["pr", "list"]:
            return {
                "success": True,
                "output": json.dumps(
                    [
                        {
                            "number": 44,
                            "state": "OPEN",
                            "url": "https://github.com/o/r/pull/44",
                            "headRefOid": "open-pr-head",
                        }
                    ]
                ),
            }
        return {"success": True, "output": ""}

    def fake_gh_merged(args: list[str], **_kw: Any) -> dict[str, Any]:
        if args[:2] == ["pr", "list"]:
            return {
                "success": True,
                "output": json.dumps(
                    [
                        {
                            "number": 44,
                            "state": "MERGED",
                            "url": "https://github.com/o/r/pull/44",
                            "headRefOid": "merged-pr-head",
                        }
                    ]
                ),
            }
        return {"success": True, "output": ""}

    with (
        patch("server.routes.github._use_provider_api", return_value=False),
        patch("server.services.approval.run_gh_command", fake_gh_open),
        patch("server.routes.github.run_gh_command", fake_gh_open),
    ):
        # Missing QA evidence -> OPEN PR refused with 409
        code, body = await _call_manual_create_pr(env["task_id"])
        assert code == 409
        assert body["reason"] == "qa_missing"

        # Evidence for a different head -> OPEN PR refused with qa_stale
        _write_gate_files(env["spec_dir"], head_sha="other-head")
        code, body = await _call_manual_create_pr(env["task_id"])
        assert code == 409
        assert body["reason"] == "qa_stale"

        # Evidence matching the OPEN PR head -> 200 existing
        _write_gate_files(env["spec_dir"], head_sha="open-pr-head")
        code, body = await _call_manual_create_pr(env["task_id"])
        assert code == 200
        assert body["data"]["existing"] is True

    # MERGED PR is never blocked even without QA evidence
    (env["spec_dir"] / "qa_evidence.json").unlink()
    with (
        patch("server.routes.github._use_provider_api", return_value=False),
        patch("server.services.approval.run_gh_command", fake_gh_merged),
        patch("server.routes.github.run_gh_command", fake_gh_merged),
    ):
        code, body = await _call_manual_create_pr(env["task_id"])
        assert code == 200
        assert body["data"]["existing"] is True
        assert body["data"]["prNumber"] == 44


@pytest.mark.asyncio
async def test_manual_route_unreadable_existing_head_is_stale(tmp_path: Path) -> None:
    env = _setup_manual_repos(tmp_path)
    _write_gate_files(env["spec_dir"], head_sha=env["head_sha"])

    def fake_gh(args: list[str], **_kw: Any) -> dict[str, Any]:
        if args[:2] == ["pr", "list"]:
            return {
                "success": True,
                "output": json.dumps(
                    [
                        {
                            "number": 44,
                            "state": "OPEN",
                            "url": "https://github.com/o/r/pull/44",
                        }
                    ]
                ),
            }
        return {"success": False, "output": ""}

    with (
        patch("server.routes.github._use_provider_api", return_value=False),
        patch("server.services.approval.run_gh_command", fake_gh),
        patch("server.routes.github.run_gh_command", fake_gh),
    ):
        code, body = await _call_manual_create_pr(env["task_id"])
    assert code == 409
    assert body["reason"] == "qa_stale"


@pytest.mark.asyncio
@pytest.mark.parametrize("use_provider_api", [False, True])
async def test_manual_route_gates_new_pr_on_github_and_provider_api(
    tmp_path: Path, use_provider_api: bool
) -> None:
    env = _setup_manual_repos(tmp_path)

    class FakeProvider:
        provider_type = "gitlab"
        created = False

        async def create_pr(self, **_kw: Any) -> dict[str, Any]:
            self.created = True
            return {"number": 88, "web_url": "https://gitlab.example/mr/88"}

    provider = FakeProvider()

    def fake_gh(args: list[str], **_kw: Any) -> dict[str, Any]:
        if args[:2] == ["pr", "list"]:
            return {"success": True, "output": "[]"}
        if args[:2] == ["pr", "create"]:
            return {"success": True, "output": "https://github.com/o/r/pull/88"}
        return {"success": True, "output": ""}

    with (
        patch("server.routes.github._use_provider_api", return_value=use_provider_api),
        patch("server.routes.github._get_project_provider", return_value=provider),
        patch("server.services.approval.run_gh_command", fake_gh),
        patch("server.routes.github.run_gh_command", fake_gh),
    ):
        # Gate fails (not independent) -> 409 before push/create
        _write_gate_files(
            env["spec_dir"],
            head_sha=env["head_sha"],
            reviewer_model="sonnet",
        )
        code, body = await _call_manual_create_pr(env["task_id"])
        assert code == 409
        assert body["reason"] == "qa_not_independent"
        assert provider.created is False

        # Gate passes -> 200
        _write_gate_files(
            env["spec_dir"],
            head_sha=env["head_sha"],
            reviewer_model="opus",
        )
        code, body = await _call_manual_create_pr(env["task_id"])
        assert code == 200
        assert body["data"]["prNumber"] == 88


@pytest.mark.asyncio
async def test_manual_route_pushes_reviewed_head_when_base_moves(
    tmp_path: Path,
) -> None:
    env = _setup_manual_repos(tmp_path)
    _write_gate_files(env["spec_dir"], head_sha=env["head_sha"])

    # Advance origin/main so rebasing onto it would rewrite the task commit SHA
    (env["project"] / "other.txt").write_text("advance main\n")
    _git(env["project"], "add", "other.txt")
    _git(env["project"], "commit", "-m", "advance main")
    _git(env["project"], "push", "origin", "main")

    def fake_gh(args: list[str], **_kw: Any) -> dict[str, Any]:
        if args[:2] == ["pr", "list"]:
            return {"success": True, "output": "[]"}
        return {"success": True, "output": "https://github.com/o/r/pull/99"}

    with (
        patch("server.routes.github._use_provider_api", return_value=False),
        patch("server.services.approval.run_gh_command", fake_gh),
        patch("server.routes.github.run_gh_command", fake_gh),
    ):
        code, body = await _call_manual_create_pr(env["task_id"])
        assert code == 200
        assert body["data"]["prNumber"] == 99
        assert _git(env["worktree"], "rev-parse", "HEAD") == env["head_sha"]
        assert _git(env["origin"], "rev-parse", env["branch"]) == env["head_sha"]

        _write_gate_files(env["spec_dir"], head_sha="old-review")
        code, body = await _call_manual_create_pr(env["task_id"])
        assert code == 409
        assert body["reason"] == "qa_stale"
