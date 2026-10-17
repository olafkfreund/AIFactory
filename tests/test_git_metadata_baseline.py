"""Tests for git metadata baseline recording and verification (#1746, #1747)."""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from core.git_metadata import (
    _baseline_path,
    check_git_metadata,
    record_baseline,
    snapshot,
)

_BACKEND_DIR = Path(__file__).resolve().parent.parent / "apps" / "backend"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed test helper argv
        ["git", *args],  # noqa: S607
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "Test")
    f = r / "f"
    f.write_text("init\n")
    _git(r, "add", "f")
    _git(r, "commit", "-q", "-m", "init")
    return r


def test_record_baseline_and_check_passes(repo: Path) -> None:
    record_baseline(repo)
    check_git_metadata(repo)
    snap = snapshot(repo)
    bpath = _baseline_path(str(snap["repo"]))
    assert bpath.exists()
    assert (bpath.stat().st_mode & 0o777) == 0o600


def test_config_merge_driver_append_raises_without_leaking_value(repo: Path) -> None:
    record_baseline(repo)
    with open(repo / ".git" / "config", "a", encoding="utf-8") as fh:
        fh.write('[merge "evil"]\n\tdriver = touch x\n')

    with pytest.raises(RuntimeError) as exc_info:
        check_git_metadata(repo)
    msg = str(exc_info.value)
    assert "merge.evil.driver" in msg
    assert "touch x" not in msg


def test_info_attributes_write_raises(repo: Path) -> None:
    record_baseline(repo)
    attrs = repo / ".git" / "info" / "attributes"
    attrs.parent.mkdir(parents=True, exist_ok=True)
    attrs.write_text("* merge=evil\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match=r"info/attributes"):
        check_git_metadata(repo)


def test_benign_git_operations_pass(repo: Path) -> None:
    record_baseline(repo)
    _git(repo, "config", "user.name", "other")
    _git(repo, "branch", "x")
    lock = repo / ".git" / "index.lock"
    lock.write_text("", encoding="utf-8")
    lock.unlink()

    check_git_metadata(repo)


def test_filter_lfs_clean_change_raises(repo: Path) -> None:
    _git(repo, "config", "filter.lfs.clean", "git-lfs clean -- %f")
    record_baseline(repo)
    _git(repo, "config", "filter.lfs.clean", "evil-clean %f")

    with pytest.raises(RuntimeError, match=r"filter\.lfs\.clean"):
        check_git_metadata(repo)


def test_missing_baseline_records_late_then_compares(
    repo: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("WARNING"):
        check_git_metadata(repo)
    assert "git metadata baseline taken late" in caplog.text

    with open(repo / ".git" / "config", "a", encoding="utf-8") as fh:
        fh.write('[merge "evil"]\n\tdriver = touch x\n')

    with pytest.raises(RuntimeError, match=r"merge\.evil\.driver"):
        check_git_metadata(repo)


def test_corrupt_baseline_file_raises(repo: Path) -> None:
    record_baseline(repo)
    snap = snapshot(repo)
    bpath = _baseline_path(str(snap["repo"]))
    bpath.write_text("{", encoding="utf-8")

    with pytest.raises(RuntimeError):
        check_git_metadata(repo)


def test_cli_rebaseline_allows_subsequent_check(repo: Path) -> None:
    record_baseline(repo)
    with open(repo / ".git" / "config", "a", encoding="utf-8") as fh:
        fh.write('[merge "evil"]\n\tdriver = touch x\n')

    with pytest.raises(RuntimeError):
        check_git_metadata(repo)

    env = dict(os.environ)
    res = subprocess.run(  # noqa: S603 - fixed CLI test invocation
        [sys.executable, "-m", "core.git_metadata", "--rebaseline", str(repo)],
        cwd=_BACKEND_DIR,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert str(repo) in res.stdout
    check_git_metadata(repo)


def test_linked_worktree_shares_baseline(repo: Path, tmp_path: Path) -> None:
    record_baseline(repo)
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", "-b", "wt-branch", str(wt))

    check_git_metadata(wt)
    check_git_metadata(repo)

    _git(wt, "config", "merge.wt.driver", "true")
    with pytest.raises(RuntimeError, match=r"merge\.wt\.driver"):
        check_git_metadata(repo)
    with pytest.raises(RuntimeError, match=r"merge\.wt\.driver"):
        check_git_metadata(wt)


def test_create_worktree_records_baseline_without_overwriting(repo: Path) -> None:
    from core.worktree import WorktreeManager

    mgr = WorktreeManager(repo)
    mgr.setup()
    mgr.create_worktree("t")
    check_git_metadata(repo)

    with open(repo / ".git" / "config", "a", encoding="utf-8") as fh:
        fh.write('[merge "evil"]\n\tdriver = touch x\n')

    mgr.create_worktree("u")
    with pytest.raises(RuntimeError, match=r"merge\.evil\.driver"):
        check_git_metadata(repo)


def test_planted_driver_and_attributes_refused_by_merge_paths(
    repo: Path, tmp_path: Path
) -> None:
    import core.workspace.merge as merge_mod
    from core.child_env import merge_driver_pins
    from core.worktree import WorktreeManager

    ws = str(Path(__file__).resolve().parent.parent / "apps" / "web-server")
    if ws not in sys.path:
        sys.path.insert(0, ws)
    from server.services import pr_endgame

    marker = tmp_path / "pwned"
    mgr = WorktreeManager(repo)
    mgr.setup()
    wt = mgr.create_worktree("t")

    (wt.path / "f").write_text("theirs\n", encoding="utf-8")
    _git(wt.path, "commit", "-q", "-am", "theirs")
    (repo / "f").write_text("ours\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "ours")

    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(repo), str(clone))
    _git(clone, "config", "user.email", "t@example.com")
    _git(clone, "config", "user.name", "Test")
    _git(clone, "checkout", "-q", "aifactory/t")
    record_baseline(clone)

    for target in (repo, clone):
        with open(target / ".git" / "config", "a", encoding="utf-8") as fh:
            fh.write(f'[merge "evil"]\n\tdriver = touch {marker}; exit 0\n')
        attrs = target / ".git" / "info" / "attributes"
        attrs.parent.mkdir(parents=True, exist_ok=True)
        attrs.write_text("* merge=evil\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match=r"merge\.evil\.driver"):
        merge_driver_pins(repo)

    conflict_res = merge_mod._check_git_conflicts(repo, "t")
    assert conflict_res["has_conflicts"] is True
    assert not marker.exists()

    pr_res = pr_endgame.resolve_pr_conflicts(
        str(clone),
        "main",
        fixer=lambda _files, _wt: False,
        runner=pr_endgame._default_runner,
    )
    assert pr_res.resolved is False
    assert "refused" in pr_res.reason
    assert not marker.exists()


def test_merge_driver_pins_fake_runner_zero_empty_succeeds(tmp_path: Path) -> None:
    from core.child_env import merge_driver_pins

    assert merge_driver_pins(tmp_path, run=lambda _argv: (0, "")) == []
    assert merge_driver_pins(tmp_path, run=lambda _argv: (0, "")) == []


@pytest.mark.asyncio
async def test_followup_planner_fails_on_git_metadata_change(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agents.planner as planner_mod  # noqa: PLC0415
    import prompts as prompts_mod  # noqa: PLC0415

    record_baseline(repo)
    spec_dir = repo / ".aifactory" / "specs" / "001-test"
    spec_dir.mkdir(parents=True)
    (spec_dir / "implementation_plan.json").write_text(
        json.dumps(
            {
                "feature": "test",
                "workflow_type": "feature",
                "phases": [
                    {
                        "phase": 1,
                        "name": "Phase 1",
                        "type": "implementation",
                        "subtasks": [
                            {
                                "id": "subtask-1-1",
                                "description": "do work",
                                "status": "pending",
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        planner_mod, "create_client", lambda *_a, **_kw: contextlib.AsyncExitStack()
    )
    monkeypatch.setattr(prompts_mod, "get_followup_planner_prompt", lambda _s: "prompt")

    async def _planted_session(
        *_a: object, **_kw: object
    ) -> tuple[str, str, dict[str, object]]:
        cfg = repo / ".git" / "config"
        cfg.write_text(
            cfg.read_text(encoding="utf-8") + '[merge "evil"]\n\tdriver = touch x\n',
            encoding="utf-8",
        )
        return ("complete", "", {})

    monkeypatch.setattr(planner_mod, "run_agent_session", _planted_session)

    ok = await planner_mod.run_followup_planner(
        repo, spec_dir, model="claude-sonnet-4-5-20250929"
    )
    assert ok is False


@pytest.mark.asyncio
async def test_coder_metadata_change_in_final_session_ends_in_error(
    tmp_path: Path,
) -> None:
    from unittest.mock import AsyncMock, MagicMock, patch  # noqa: PLC0415

    import agents.coder as coder  # noqa: PLC0415
    from ui import BuildState  # noqa: PLC0415

    spec_dir = tmp_path / "001-spec"
    spec_dir.mkdir()
    (spec_dir / "spec.md").write_text("# Feature\n", encoding="utf-8")
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    status_cls = MagicMock()
    gates = AsyncMock()
    scaffold = MagicMock(return_value=[])
    patchers = {
        "create_client": MagicMock(return_value=client),
        "run_session_guarded": AsyncMock(return_value=("continue", "", {})),
        "check_git_metadata": MagicMock(side_effect=RuntimeError("merge.evil")),
        "_run_trailing_gates_if_build_complete": gates,
        "scaffold_deploy_for_spec": scaffold,
        "get_graphiti_context": AsyncMock(return_value=""),
        "RecoveryManager": MagicMock(),
        "StatusManager": status_cls,
        "get_task_logger": MagicMock(return_value=None),
        "debug_memory_system_status": MagicMock(),
        "record_turn": MagicMock(),
        "CompactionDetector": MagicMock(),
        "build_operational_context": MagicMock(return_value=""),
        "drain_inbox": MagicMock(return_value=[]),
        "is_build_complete": MagicMock(return_value=True),
        "print_build_complete_banner": MagicMock(),
        "print_progress_summary": MagicMock(),
        "print_session_header": MagicMock(),
        "count_subtasks": MagicMock(return_value=(3, 3)),
        "count_subtasks_detailed": MagicMock(
            return_value={"completed": 3, "total": 3, "in_progress": 0}
        ),
        "get_current_phase": MagicMock(return_value=None),
        "get_next_subtask": MagicMock(return_value=None),
        "get_latest_commit": MagicMock(return_value="abc"),
        "get_commit_count": MagicMock(return_value=0),
        "infer_provider_from_model": MagicMock(return_value="claude"),
        "get_phase_model": MagicMock(return_value="claude-sonnet-4-5"),
        "get_phase_thinking_budget": MagicMock(return_value=None),
    }
    with contextlib.ExitStack() as stack:
        for name, repl in patchers.items():
            stack.enter_context(patch.object(coder, name, repl))
        await coder.run_autonomous_agent(
            project_dir=tmp_path,
            spec_dir=spec_dir,
            model="claude-sonnet-4-5",
            max_iterations=1,
        )

    states = [
        c.kwargs.get("state") for c in status_cls.return_value.update.call_args_list
    ]
    assert BuildState.ERROR in states
    assert BuildState.COMPLETE not in states
    assert states[-1] == BuildState.ERROR
    gates.assert_not_called()
    scaffold.assert_not_called()


def test_parallel_checks_git_metadata_after_session() -> None:
    parallel_src = (_BACKEND_DIR / "agents" / "parallel_integration.py").read_text(
        encoding="utf-8"
    )
    assert re.search(
        r"await run_session_guarded\([\s\S]{0,300}?"
        r"await asyncio\.to_thread\(check_git_metadata,\s*child_path\)"
        r"[\s\S]{0,200}?except RuntimeError as exc:"
        r"[\s\S]{0,200}?session_ok\s*=\s*False",
        parallel_src,
    )
