"""Fresh QA verdicts and per-run protected evidence."""

import contextlib
import hashlib
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import core.workspace_fetch as workspace_fetch
import pytest
from agents.token_attribution import PromptSegments, TurnUsage, record_turn
from qa import fixer, loop, reviewer


def test_skip_qa_build_clears_previous_approval(monkeypatch, tmp_path):
    from cli import build_commands

    spec = tmp_path / "spec"
    spec.mkdir()
    (spec / "spec.md").write_text("# task")
    for name in ("qa_evidence.json", "qa_evidence_report.md"):
        (spec / name).write_text("old approval")
    (spec / ".qa_evidence_run").write_text("old packed run")
    (spec / "model_identities.json").write_text('[{"model": "sonnet"}]')
    seen = []

    async def stop_before_agent(*_args, **_kwargs):
        seen.append(
            all(
                not (spec / name).exists()
                for name in (
                    "qa_evidence.json",
                    "qa_evidence_report.md",
                    ".qa_evidence_run",
                )
            )
        )
        raise RuntimeError("stop before coding")

    monkeypatch.delenv("WORKSPACE_URI", raising=False)
    with (
        patch("agent.run_autonomous_agent", new=stop_before_agent),
        patch("cli.utils.validate_environment", return_value=True),
        patch("cli.utils.print_banner"),
        patch.object(
            build_commands,
            "choose_workspace",
            return_value=build_commands.WorkspaceMode.DIRECT,
        ),
        patch.object(build_commands, "get_existing_build_worktree", return_value=None),
        patch.object(build_commands, "ReviewState", MagicMock()),
        patch.object(build_commands, "_record_git_baseline_early"),
        contextlib.suppress(RuntimeError, SystemExit),
    ):
        build_commands.handle_build_command(
            project_dir=tmp_path,
            spec_dir=spec,
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
    assert json.loads((spec / "model_identities.json").read_text()) == [
        {"model": "sonnet"}
    ]


class Store:
    def __init__(self):
        self.objects = {}

    def put_bytes(self, key, data, *_args, **_kwargs):
        self.objects[key] = data

    def get_bytes(self, key):
        return self.objects[key]


def test_evidence_uses_worktree_report_but_only_source_receives_protected_copy(
    monkeypatch, tmp_path
):
    worktree = tmp_path / "worktree"
    source = tmp_path / "source"
    worktree.mkdir()
    source.mkdir()
    (worktree / "qa_report.md").write_text("reviewed report")
    (worktree / "qa_evidence.json").write_text('{"status":"forged"}')
    monkeypatch.setattr(loop.subprocess, "check_output", lambda *_a, **_k: "head-a\n")

    assert loop._write_evidence(
        worktree,
        worktree,
        source,
        status="approved",
        model="claude-opus-4-5",
        provider="claude",
        cycle=2,
    )
    evidence = json.loads((source / "qa_evidence.json").read_text())
    assert evidence["status"] == "approved"
    assert evidence["reviewed_head"] == "head-a"
    assert evidence["report_sha256"] == hashlib.sha256(b"reviewed report").hexdigest()
    assert (source / "qa_evidence_report.md").read_text() == "reviewed report"
    assert json.loads((worktree / "qa_evidence.json").read_text()) == {
        "status": "forged"
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fresh,text_only", [(False, False), (True, False), (True, True)]
)
async def test_cycle_clears_old_evidence_and_requires_fresh_signoff(
    monkeypatch, tmp_path, fresh, text_only
):
    source = tmp_path / "source"
    source.mkdir()
    spec = tmp_path / "worktree"
    spec.mkdir()
    (spec / "implementation_plan.json").write_text(
        json.dumps({"qa_signoff": {"status": "approved", "qa_session": 0}})
    )
    for name in ("qa_evidence.json", "qa_evidence_report.md"):
        (source / name).write_text("stale")
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(loop, "MAX_QA_ITERATIONS", 1)
    monkeypatch.setattr(loop, "is_build_complete", lambda *_a: True)
    monkeypatch.setattr(loop, "get_task_logger", lambda *_a: None)
    monkeypatch.setattr(loop, "is_no_test_project", lambda *_a: False)
    monkeypatch.setattr(loop, "_create_qa_reviewer_provider", lambda *_a, **_k: client)

    async def review(*_args, **_kwargs):
        if fresh:
            (spec / "implementation_plan.json").write_text(
                json.dumps({"qa_signoff": {"status": "approved"}})
            )
            if not text_only:
                (spec / "qa_report.md").write_text("fresh report")
        return "approved", "text-only report"

    monkeypatch.setattr(loop, "run_qa_agent_session", review)
    monkeypatch.setattr(loop.subprocess, "check_output", lambda *_a, **_k: "head-b\n")
    monkeypatch.setattr(loop, "request_review", lambda *_a: MagicMock(cycle_id=1))
    monkeypatch.setattr(loop, "check_git_metadata", lambda *_a: None)
    monkeypatch.setattr(loop, "record_started", lambda *_a, **_k: None)
    monkeypatch.setattr(loop, "resolve_review", lambda *_a, **_k: None)
    monkeypatch.setattr(loop, "record_iteration", lambda *_a, **_k: None)
    monkeypatch.setattr(loop, "get_iteration_history", lambda *_a: [])
    monkeypatch.setattr(
        loop, "get_recurring_issue_summary", lambda *_a: {"total_issues": 0}
    )
    gates = MagicMock()
    gates.model.return_value = "claude-opus-4-5"
    gates.thinking_budget.return_value = None

    assert (
        await loop.run_qa_validation_loop(
            tmp_path, spec, "claude-opus-4-5", source_spec_dir=source, gates=gates
        )
        is fresh
    )
    if fresh:
        assert (
            json.loads((source / "qa_evidence.json").read_text())["reviewed_head"]
            == "head-b"
        )
        report = "text-only report" if text_only else "fresh report"
        assert (spec / "qa_report.md").read_text() == report
        assert (source / "qa_evidence_report.md").read_text() == report
    else:
        assert "qa_signoff" not in json.loads(
            (spec / "implementation_plan.json").read_text()
        )
        assert not (source / "qa_evidence.json").exists()
        assert not (source / "qa_evidence_report.md").exists()


def test_packed_qa_evidence_round_trip_and_absent_verdict(monkeypatch, tmp_path):
    store = Store()
    monkeypatch.setattr("core.artifact_store.ArtifactStore", lambda: store)
    monkeypatch.setenv(workspace_fetch.WORKSPACE_URI_ENV, "s3://packed/workspace")
    monkeypatch.setenv("AIFACTORY_QA_RUN_ID", "a" * 32)
    job = tmp_path / "job"
    control = tmp_path / "control"
    job.mkdir()
    control.mkdir()
    (control / ".qa_evidence_run").write_text("a" * 32)
    for name in workspace_fetch._QA_FILES:
        (job / name).write_text(name)
    assert workspace_fetch.maybe_push_qa_evidence(job, "task-1")
    assert workspace_fetch.maybe_fetch_qa_evidence(control, "task-1")
    assert all(
        (control / name).read_text() == name for name in workspace_fetch._QA_FILES
    )

    (job / "qa_evidence.json").unlink()
    (job / "qa_evidence_report.md").unlink()
    monkeypatch.setenv("AIFACTORY_QA_RUN_ID", "b" * 32)
    (control / ".qa_evidence_run").write_text("b" * 32)
    assert workspace_fetch.maybe_push_qa_evidence(job, "task-1")
    assert workspace_fetch.maybe_fetch_qa_evidence(control, "task-1")
    assert not (control / "qa_evidence.json").exists()
    assert not (control / "qa_evidence_report.md").exists()
    assert (control / "model_identities.json").read_text() == "model_identities.json"

    (control / ".qa_evidence_run").write_text("c" * 32)
    assert not workspace_fetch.maybe_fetch_qa_evidence(control, "task-1")
    assert not (control / "model_identities.json").exists()


def test_protected_copy_is_outside_worktree_sync_allowlist():
    path = (
        Path(__file__).parent.parent
        / "apps/web-server/server/services/agent_worktree_sync.py"
    )
    source = path.read_text()
    allowlist = source.split("files_to_sync = [", 1)[1].split("]", 1)[0]
    assert '"qa_report.md"' in allowlist
    assert "qa_evidence_report.md" not in allowlist
    assert "qa_evidence.json" not in allowlist


@pytest.mark.asyncio
async def test_qa_usage_keeps_main_worker_and_uses_cycle_worker_ids(
    monkeypatch, tmp_path
):
    class ResultMessage:
        usage = {"input_tokens": 5, "output_tokens": 3}
        total_cost_usd = 0.01
        duration_ms = 200

    class Client:
        model = "claude-opus-4-5"

        async def query(self, _prompt):
            pass

        async def receive_response(self):
            yield ResultMessage()

    record_turn(
        tmp_path,
        PromptSegments(user_prompt="coding"),
        TurnUsage(input_tokens=10),
        model="claude-sonnet-4-5",
        duration_ms=100,
    )
    (tmp_path / "QA_FIX_REQUEST.md").write_text("fix")
    for module in (reviewer, fixer):
        monkeypatch.setattr(module, "get_task_logger", lambda *_a: None)
        monkeypatch.setattr(module, "get_graphiti_context", AsyncMock(return_value=""))
        monkeypatch.setattr(module, "save_session_memory", AsyncMock())
        monkeypatch.setattr(
            module, "get_qa_signoff_status", lambda *_a: {"status": "approved"}
        )
    monkeypatch.setattr(reviewer, "get_qa_reviewer_prompt", lambda *_a: "review")
    monkeypatch.setattr(fixer, "load_qa_fixer_prompt", lambda: "fix")

    assert (await reviewer.run_qa_agent_session(Client(), tmp_path, tmp_path, 2, 3))[
        0
    ] == "approved"
    assert (await fixer.run_qa_fixer_session(Client(), tmp_path, 2))[0] == "fixed"
    workers = json.loads((tmp_path / "token_usage.json").read_text())["workers"]
    assert set(workers) == {"main", "qa-2", "qa-fixer-2"}
    assert workers["main"]["duration_ms"] == 100
