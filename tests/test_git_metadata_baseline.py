"""Tests for git metadata baseline recording and verification (#1746, #1747)."""

from __future__ import annotations

import contextlib
import importlib
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
    record_worktree_baseline,
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


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("lfs.customtransfer.evil.path", "/tmp/x"),
        ("lfs.standalonetransferagent", "evil"),
        ("submodule.s.update", "!touch x"),
    ],
)
def test_added_executable_config_raises(repo: Path, key: str, value: str) -> None:
    record_baseline(repo)
    _git(repo, "config", key, value)

    with pytest.raises(RuntimeError, match=re.escape(key)):
        check_git_metadata(repo)


def test_added_non_executable_submodule_update_passes(repo: Path) -> None:
    record_baseline(repo)
    _git(repo, "config", "submodule.s.update", "checkout")

    check_git_metadata(repo)


def test_missing_baseline_records_late_then_compares(
    repo: Path, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    with caplog.at_level("WARNING"):
        check_git_metadata(repo)
    assert "git metadata baseline taken late" in caplog.text
    # Visible in the build output, not only the logger (#1832).
    out = capsys.readouterr().out
    assert "WARNING: git metadata baseline taken late" in out
    assert "not detected" in out

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


def _configured_linked_worktree(repo: Path, tmp_path: Path) -> Path:
    # The main worktree's config.worktree differs from the linked one's: under
    # the old shared baseline a check in the linked worktree reported drift.
    _git(repo, "config", "extensions.worktreeConfig", "true")
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", "-b", "wt-branch", str(wt))
    _git(repo, "config", "--worktree", "merge.x.driver", "true")
    record_baseline(repo)
    record_worktree_baseline(wt)
    return wt


def test_linked_worktree_config_does_not_trigger_shared_baseline(
    repo: Path, tmp_path: Path
) -> None:
    wt = _configured_linked_worktree(repo, tmp_path)

    check_git_metadata(wt)
    check_git_metadata(repo)


def test_linked_worktree_config_tamper_is_isolated(repo: Path, tmp_path: Path) -> None:
    wt = _configured_linked_worktree(repo, tmp_path)
    _git(wt, "config", "--worktree", "merge.y.driver", "evil")

    with pytest.raises(RuntimeError, match=r"worktree:merge\.y\.driver"):
        check_git_metadata(wt)
    check_git_metadata(repo)


def test_missing_worktree_baseline_expects_empty_config(repo: Path) -> None:
    _git(repo, "config", "extensions.worktreeConfig", "true")
    record_baseline(repo)
    _git(repo, "config", "--worktree", "core.fsmonitor", "x")

    with pytest.raises(RuntimeError, match=r"worktree:core\.fsmonitor"):
        check_git_metadata(repo)


def test_create_worktree_records_its_config_baseline(repo: Path) -> None:
    from core.worktree import WorktreeManager

    _git(repo, "config", "extensions.worktreeConfig", "true")
    _git(repo, "config", "--worktree", "merge.x.driver", "true")
    mgr = WorktreeManager(repo)
    mgr.setup()
    wt = mgr.create_worktree("worktree-config")

    check_git_metadata(wt.path)


def test_create_worktree_records_baseline_without_overwriting(repo: Path) -> None:
    from core.worktree import WorktreeManager

    mgr = WorktreeManager(repo)
    mgr.setup()
    mgr.create_worktree("t")
    # check_git_metadata would take a late baseline, so assert create_worktree wrote it.
    assert _baseline_path(str(snapshot(repo)["repo"])).exists()
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
    emit = MagicMock()
    scaffold = MagicMock(return_value=[])
    patchers = {
        "create_client": MagicMock(return_value=client),
        "run_session_guarded": AsyncMock(return_value=("continue", "", {})),
        "check_git_metadata": MagicMock(side_effect=RuntimeError("merge.evil")),
        "_run_trailing_gates_if_build_complete": gates,
        "scaffold_deploy_for_spec": scaffold,
        "emit_phase": emit,
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
    }
    with contextlib.ExitStack() as stack:
        stack.enter_context(
            patch("run_gates.get_phase_model", return_value="claude-sonnet-4-5")
        )
        stack.enter_context(
            patch("run_gates.get_phase_thinking_budget", return_value=None)
        )
        for name, repl in patchers.items():
            stack.enter_context(patch.object(coder, name, repl))
        # The caller (cli/build_commands.py) must see the tamper, or it runs QA
        # in the tampered worktree and exits 0.
        with pytest.raises(RuntimeError, match="merge.evil"):
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
    from phase_event import ExecutionPhase  # noqa: PLC0415

    assert emit.call_args.args[0] == ExecutionPhase.FAILED


@pytest.mark.asyncio
async def test_parallel_child_metadata_change_raises_after_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed child stays IN_PROGRESS and serial never reruns it, so the phase
    itself must raise, or a tampered --parallel build exits 0."""
    from unittest.mock import AsyncMock, MagicMock  # noqa: PLC0415

    import agents.parallel_integration as wave  # noqa: PLC0415
    from agents.parallel_runner import PhaseRunResult  # noqa: PLC0415
    from core.git_metadata import GitMetadataChangedError  # noqa: PLC0415
    from implementation_plan.plan import ImplementationPlan  # noqa: PLC0415

    importlib.import_module("core.worktree")
    spec_dir = tmp_path / "001-spec"
    spec_dir.mkdir()
    plan_path = spec_dir / "implementation_plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "feature": "f",
                "phases": [
                    {
                        "phase": 1,
                        "name": "P",
                        "parallel_safe": True,
                        "subtasks": [
                            {"id": "A", "description": "a", "status": "pending"},
                            {"id": "B", "description": "b", "status": "pending"},
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    child = tmp_path / "child"
    (child / ".aifactory" / "specs" / spec_dir.name).mkdir(parents=True)
    wt = MagicMock()
    wt.create_worktree.return_value = MagicMock(path=child)
    # sys.modules, not ``import core.worktree``: other tests re-import core.*,
    # leaving the package attribute stale while the local import reads sys.modules.
    monkeypatch.setattr(
        sys.modules["core.worktree"], "WorktreeManager", MagicMock(return_value=wt)
    )
    monkeypatch.setattr(
        "run_gates.get_phase_model", lambda *a, **k: "claude-sonnet-4-5"
    )
    monkeypatch.setattr("run_gates.get_phase_thinking_budget", lambda *a, **k: None)
    for name, repl in {
        "_task_branch": MagicMock(return_value="main"),
        "create_client": MagicMock(),
        "generate_subtask_prompt": MagicMock(return_value="p"),
        "load_subtask_context": MagicMock(return_value={}),
        "get_graphiti_context": AsyncMock(return_value=""),
        "_head_commit": MagicMock(return_value="abc"),
        "run_session_guarded": AsyncMock(return_value=("complete", "", {})),
        "check_git_metadata": MagicMock(
            side_effect=lambda p: (_ for _ in ()).throw(RuntimeError("merge.evil"))
        ),
        "read_test_evidence": MagicMock(return_value={}),
        "_capture_memory": AsyncMock(),
        "record_subtask_started": MagicMock(),
    }.items():
        monkeypatch.setattr(wave, name, repl)

    async def _phase(subtasks, *, run_subtask, **_kw):  # noqa: ANN001, ANN202
        res = await run_subtask(subtasks[0], index=0)
        assert res.success is False
        # B merged and completed; A failed its check and is left IN_PROGRESS.
        return PhaseRunResult(completed_ids=["B"], failed_ids=["A"])

    monkeypatch.setattr(wave, "run_parallel_phase", _phase)
    plan = ImplementationPlan.load(plan_path)
    with pytest.raises(GitMetadataChangedError, match="A: merge.evil"):
        await wave.run_parallel_coding_phase(
            plan=plan,
            phase=plan.phases[0],
            project_dir=tmp_path,
            spec_dir=spec_dir,
            model="claude-sonnet-4-5",
            workers=2,
        )


@pytest.mark.asyncio
async def test_coder_parallel_metadata_change_ends_build_in_error(
    tmp_path: Path,
) -> None:
    """The parallel raise must not be swallowed into a serial fallback."""
    from unittest.mock import AsyncMock, MagicMock, patch  # noqa: PLC0415

    import agents.coder as coder  # noqa: PLC0415
    import agents.parallel_integration as wave  # noqa: PLC0415
    from core.git_metadata import GitMetadataChangedError  # noqa: PLC0415
    from phase_event import ExecutionPhase  # noqa: PLC0415
    from ui import BuildState  # noqa: PLC0415

    spec_dir = tmp_path / "001-spec"
    spec_dir.mkdir()
    (spec_dir / "spec.md").write_text("# Feature\n", encoding="utf-8")
    (spec_dir / "implementation_plan.json").write_text(
        json.dumps(
            {
                "feature": "f",
                "phases": [
                    {
                        "phase": 1,
                        "name": "P",
                        "parallel_safe": True,
                        "subtasks": [
                            {"id": "A", "description": "a", "status": "pending"},
                            {"id": "B", "description": "b", "status": "pending"},
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    status_cls = MagicMock()
    emit = MagicMock()
    session = AsyncMock(return_value=("continue", "", {}))
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    patchers = {
        "create_client": MagicMock(return_value=client),
        "infer_provider_from_model": MagicMock(return_value="claude"),
        "run_session_guarded": session,
        "emit_phase": emit,
        "get_graphiti_context": AsyncMock(return_value=""),
        "RecoveryManager": MagicMock(),
        "StatusManager": status_cls,
        "get_task_logger": MagicMock(return_value=None),
        "debug_memory_system_status": MagicMock(),
        "is_build_complete": MagicMock(return_value=False),
        "print_progress_summary": MagicMock(),
        "print_session_header": MagicMock(),
        "count_subtasks": MagicMock(return_value=(0, 2)),
        "get_next_subtask": MagicMock(return_value={"id": "A", "status": "pending"}),
        "get_latest_commit": MagicMock(return_value="abc"),
        "get_commit_count": MagicMock(return_value=0),
    }
    with contextlib.ExitStack() as stack:
        stack.enter_context(
            patch("run_gates.get_phase_model", return_value="claude-sonnet-4-5")
        )
        stack.enter_context(
            patch("run_gates.get_phase_thinking_budget", return_value=None)
        )
        for name, repl in patchers.items():
            stack.enter_context(patch.object(coder, name, repl))
        stack.enter_context(
            patch.object(
                wave,
                "run_parallel_coding_phase",
                AsyncMock(side_effect=GitMetadataChangedError("A: merge.evil")),
            )
        )
        with pytest.raises(GitMetadataChangedError, match="merge.evil"):
            await coder.run_autonomous_agent(
                project_dir=tmp_path,
                spec_dir=spec_dir,
                model="claude-sonnet-4-5",
                max_iterations=1,
                parallel=True,
                workers=2,
            )

    session.assert_not_called()
    states = [
        c.kwargs.get("state") for c in status_cls.return_value.update.call_args_list
    ]
    assert states[-1] == BuildState.ERROR
    assert emit.call_args.args[0] == ExecutionPhase.FAILED


def test_qa_fixer_metadata_change_fails_build_nonzero(
    repo: Path, tmp_path: Path
) -> None:
    from unittest.mock import AsyncMock, MagicMock, patch  # noqa: PLC0415

    import qa_loop  # noqa: PLC0415
    from cli import build_commands  # noqa: PLC0415
    from phase_event import ExecutionPhase  # noqa: PLC0415

    # Patch the globals of the loop build_commands will call: other test files
    # evict and re-import qa.loop at collection time.
    loop_globals = qa_loop.run_qa_validation_loop.__globals__

    record_baseline(repo)
    spec_dir = tmp_path / "001-spec"
    spec_dir.mkdir()
    (spec_dir / "spec.md").write_text("# Feature\n", encoding="utf-8")
    # Human feedback makes the loop run the fixer before the reviewer.
    (spec_dir / "QA_FIX_REQUEST.md").write_text("fix it\n", encoding="utf-8")

    async def fixer(*_a, **_k):
        _git(repo, "config", "core.fsmonitor", "/tmp/evil")
        return "fixed", ""

    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    emit = MagicMock()
    review_state = MagicMock()
    review_state.is_approval_valid.return_value = True
    patches = [
        patch("agent.run_autonomous_agent", new=AsyncMock(return_value=None)),
        patch("agent.sync_plan_to_source", MagicMock(return_value=False)),
        patch("qa_loop.should_run_qa", MagicMock(return_value=True)),
        patch("cli.utils.validate_environment", MagicMock(return_value=True)),
        patch("cli.utils.print_banner", MagicMock()),
        patch.object(
            build_commands,
            "choose_workspace",
            MagicMock(return_value=build_commands.WorkspaceMode.DIRECT),
        ),
        patch.object(
            build_commands, "get_existing_build_worktree", MagicMock(return_value=None)
        ),
        patch.object(
            build_commands, "build_is_silent_noop", MagicMock(return_value=False)
        ),
        patch.object(
            build_commands,
            "ReviewState",
            MagicMock(load=MagicMock(return_value=review_state)),
        ),
        patch("phase_event.emit_phase", emit),
        patch("agents.coder.emit_phase", emit),
        patch.dict(
            loop_globals,
            {
                "emit_phase": emit,
                "is_build_complete": MagicMock(return_value=True),
                "is_no_test_project": MagicMock(return_value=False),
                "get_task_logger": MagicMock(return_value=None),
                "create_client": MagicMock(return_value=client),
                "run_qa_fixer_session": fixer,
                "_create_qa_reviewer_provider": MagicMock(return_value=client),
                "run_qa_agent_session": AsyncMock(return_value=("approved", "ok")),
            },
        ),
    ]
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        with pytest.raises(SystemExit) as exc:
            build_commands.handle_build_command(
                project_dir=repo,
                spec_dir=spec_dir,
                model="claude-sonnet-4-5",
                max_iterations=1,
                verbose=False,
                force_isolated=False,
                force_direct=True,
                auto_continue=True,
                skip_qa=False,
                force_bypass_approval=False,
            )

    assert exc.value.code == 1
    phases = [c.args[0] for c in emit.call_args_list]
    assert ExecutionPhase.FAILED in phases
    assert ExecutionPhase.COMPLETE not in phases


def test_qa_post_loop_metadata_change_fails_build_nonzero(
    repo: Path, tmp_path: Path
) -> None:
    from unittest.mock import AsyncMock, MagicMock, patch  # noqa: PLC0415

    import qa_loop  # noqa: PLC0415
    from cli import build_commands  # noqa: PLC0415
    from phase_event import ExecutionPhase  # noqa: PLC0415

    loop_globals = qa_loop.run_qa_validation_loop.__globals__
    record_baseline(repo)
    spec_dir = tmp_path / "001-spec"
    spec_dir.mkdir()
    (spec_dir / "spec.md").write_text("# Feature\n", encoding="utf-8")
    (spec_dir / "QA_FIX_REQUEST.md").write_text("fix it\n", encoding="utf-8")

    async def fixer(*_a, **_k):
        _git(repo, "config", "core.fsmonitor", "/tmp/evil")
        return "fixed", ""

    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    emit = MagicMock()
    reviewer = AsyncMock(return_value=("approved", "ok"))
    review_state = MagicMock()
    review_state.is_approval_valid.return_value = True
    patches = [
        patch("agent.run_autonomous_agent", new=AsyncMock(return_value=None)),
        patch("agent.sync_plan_to_source", MagicMock(return_value=False)),
        patch("qa_loop.should_run_qa", MagicMock(return_value=True)),
        patch("cli.utils.validate_environment", MagicMock(return_value=True)),
        patch("cli.utils.print_banner", MagicMock()),
        patch.object(
            build_commands,
            "choose_workspace",
            MagicMock(return_value=build_commands.WorkspaceMode.DIRECT),
        ),
        patch.object(
            build_commands, "get_existing_build_worktree", MagicMock(return_value=None)
        ),
        patch.object(
            build_commands, "build_is_silent_noop", MagicMock(return_value=False)
        ),
        patch.object(
            build_commands,
            "ReviewState",
            MagicMock(load=MagicMock(return_value=review_state)),
        ),
        patch("phase_event.emit_phase", emit),
        patch("agents.coder.emit_phase", emit),
        patch.dict(
            loop_globals,
            {
                "emit_phase": emit,
                "is_build_complete": MagicMock(return_value=True),
                "is_no_test_project": MagicMock(return_value=False),
                "get_task_logger": MagicMock(return_value=None),
                "create_client": MagicMock(return_value=client),
                "run_qa_fixer_session": fixer,
                "get_qa_iteration_count": MagicMock(
                    return_value=loop_globals["MAX_QA_ITERATIONS"]
                ),
                "_create_qa_reviewer_provider": MagicMock(return_value=client),
                "run_qa_agent_session": reviewer,
            },
        ),
    ]
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        with pytest.raises(SystemExit) as exc:
            build_commands.handle_build_command(
                project_dir=repo,
                spec_dir=spec_dir,
                model="claude-sonnet-4-5",
                max_iterations=1,
                verbose=False,
                force_isolated=False,
                force_direct=True,
                auto_continue=True,
                skip_qa=False,
                force_bypass_approval=False,
            )

    assert exc.value.code == 1
    phases = [c.args[0] for c in emit.call_args_list]
    assert ExecutionPhase.FAILED in phases
    assert ExecutionPhase.COMPLETE not in phases
    reviewer.assert_not_awaited()


def test_qa_preapproved_metadata_change_fails_build_nonzero(
    repo: Path, tmp_path: Path
) -> None:
    from unittest.mock import AsyncMock, MagicMock, patch  # noqa: PLC0415

    from cli import build_commands  # noqa: PLC0415
    from phase_event import ExecutionPhase  # noqa: PLC0415

    record_baseline(repo)
    spec_dir = tmp_path / "001-spec"
    spec_dir.mkdir()
    (spec_dir / "spec.md").write_text("# Feature\n", encoding="utf-8")
    _git(repo, "config", "core.fsmonitor", "/tmp/evil")

    emit = MagicMock()
    review_state = MagicMock()
    review_state.is_approval_valid.return_value = True
    patches = [
        patch("agent.run_autonomous_agent", new=AsyncMock(return_value=None)),
        patch("agent.sync_plan_to_source", MagicMock(return_value=False)),
        patch("qa_loop.should_run_qa", MagicMock(return_value=False)),
        patch("qa_loop.is_qa_approved", MagicMock(return_value=True)),
        patch("cli.utils.validate_environment", MagicMock(return_value=True)),
        patch("cli.utils.print_banner", MagicMock()),
        patch.object(
            build_commands,
            "choose_workspace",
            MagicMock(return_value=build_commands.WorkspaceMode.DIRECT),
        ),
        patch.object(
            build_commands, "get_existing_build_worktree", MagicMock(return_value=None)
        ),
        patch.object(
            build_commands, "build_is_silent_noop", MagicMock(return_value=False)
        ),
        patch.object(
            build_commands,
            "ReviewState",
            MagicMock(load=MagicMock(return_value=review_state)),
        ),
        patch("phase_event.emit_phase", emit),
        patch("agents.coder.emit_phase", emit),
    ]
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        with pytest.raises(SystemExit) as exc:
            build_commands.handle_build_command(
                project_dir=repo,
                spec_dir=spec_dir,
                model="claude-sonnet-4-5",
                max_iterations=1,
                verbose=False,
                force_isolated=False,
                force_direct=True,
                auto_continue=True,
                skip_qa=False,
                force_bypass_approval=False,
            )

    assert exc.value.code == 1
    phases = [c.args[0] for c in emit.call_args_list]
    assert ExecutionPhase.FAILED in phases
    assert ExecutionPhase.COMPLETE not in phases


def _baseline_exists(repo: Path) -> bool:
    return _baseline_path(str(snapshot(repo)["repo"])).exists()


def test_direct_build_records_baseline_before_first_agent(
    repo: Path, tmp_path: Path
) -> None:
    """Direct mode never calls create_worktree; the build baselines first (#1832)."""
    from unittest.mock import MagicMock, patch  # noqa: PLC0415

    from cli import build_commands  # noqa: PLC0415

    spec_dir = tmp_path / "001-spec"
    spec_dir.mkdir()
    (spec_dir / "spec.md").write_text("# Feature\n", encoding="utf-8")
    seen: list[bool] = []

    async def agent(*_a, **_k):
        seen.append(_baseline_exists(repo))
        raise RuntimeError("stop after the first agent")

    patches = [
        patch("agent.run_autonomous_agent", new=agent),
        patch("cli.utils.validate_environment", MagicMock(return_value=True)),
        patch("cli.utils.print_banner", MagicMock()),
        patch.object(
            build_commands,
            "choose_workspace",
            MagicMock(return_value=build_commands.WorkspaceMode.DIRECT),
        ),
        patch.object(
            build_commands, "get_existing_build_worktree", MagicMock(return_value=None)
        ),
        patch.object(
            build_commands,
            "ReviewState",
            MagicMock(load=MagicMock(return_value=MagicMock())),
        ),
    ]
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        with contextlib.suppress(SystemExit, RuntimeError):
            build_commands.handle_build_command(
                project_dir=repo,
                spec_dir=spec_dir,
                model="claude-sonnet-4-5",
                max_iterations=1,
                verbose=False,
                force_isolated=False,
                force_direct=True,
                auto_continue=True,
                skip_qa=True,
                force_bypass_approval=False,
            )

    assert seen == [True]


def test_spec_runner_records_baseline_before_spec_agents(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec-pipeline agents run before create_worktree; baseline first (#1832)."""
    from unittest.mock import MagicMock  # noqa: PLC0415

    from runners import spec_runner  # noqa: PLC0415

    seen: list[bool] = []

    async def run(**_k: object) -> bool:
        seen.append(_baseline_exists(repo))
        return False

    (repo / ".aifactory").mkdir()  # stop main()'s project-root walk at repo
    orch = MagicMock()
    orch.run = run
    monkeypatch.setattr(spec_runner, "SpecOrchestrator", MagicMock(return_value=orch))
    monkeypatch.setattr(
        sys, "argv", ["spec_runner.py", "--task", "x", "--project-dir", str(repo)]
    )
    with pytest.raises(SystemExit):
        spec_runner.main()

    assert seen == [True]
