"""The runner publishes a question only after checkpoint and transport succeed."""

import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from agents import coder
from agents.parallel_integration import _write_merged_question
from agents.parallel_runner import SubtaskResult, run_parallel_phase
from agents.questions import (
    QuestionCheckpointError,
    QuestionPause,
    create_question,
    list_questions,
    question_run_id,
    start_question_run,
    update_question,
    write_question,
)
from core import artifact_store, workspace_fetch
from implementation_plan.subtask import Subtask
from ui import BuildState


def _question(spec_dir: Path) -> str:
    run_id = start_question_run(spec_dir)
    return write_question(
        spec_dir, question="Which format?", run_id=run_id, asked_by_model="claude"
    )["id"]


def _status():
    states = []
    return SimpleNamespace(states=states, update=lambda *, state: states.append(state))


@pytest.fixture(autouse=True)
def clean_run_env(monkeypatch):
    monkeypatch.delenv("AIFACTORY_ATTEMPT_ID", raising=False)
    monkeypatch.delenv("WORKSPACE_URI", raising=False)


@pytest.mark.parametrize("attempt_id", ["a" * 32, "A" * 32, None])
def test_start_question_run_uses_only_valid_attempt_id(
    tmp_path, monkeypatch, attempt_id
):
    if attempt_id is not None:
        monkeypatch.setenv("AIFACTORY_ATTEMPT_ID", attempt_id)
    run_id = start_question_run(tmp_path)
    assert question_run_id(tmp_path) == run_id
    if attempt_id == "a" * 32:
        assert run_id == attempt_id
    else:
        assert len(run_id) == 32
        assert all(char in "0123456789abcdef" for char in run_id)
        assert start_question_run(tmp_path) != run_id


def test_local_pause_copies_only_question_json_after_checkpoint(tmp_path, monkeypatch):
    spec_dir = tmp_path / "work" / "spec"
    source = tmp_path / "source"
    source.mkdir()
    qid = _question(spec_dir)
    (source / "review_state.json").write_text('{"approved": true, "approved_by": "x"}')
    (spec_dir / "review_state.json").write_text(
        '{"approved": false, "approved_by": "forged"}'
    )
    (spec_dir / "implementation_plan.json").write_text(
        json.dumps({"phases": [{"subtasks": [{"id": "1", "status": "in_progress"}]}]})
    )
    calls = []
    monkeypatch.setattr(
        coder, "_checkpoint_question_branch", lambda *_: calls.append("checkpoint")
    )
    monkeypatch.setattr(coder, "sync_plan_to_source", lambda *_: True)
    status = _status()
    coder._pause_for_question(tmp_path, spec_dir, source, status)
    assert status.states[-1] == BuildState.PAUSED
    assert calls == ["checkpoint"]
    assert [q["id"] for q in list_questions(source)] == [qid]
    assert not (source / "questions" / ".lock").exists()
    assert not (source / "questions" / ".budget.json").exists()
    assert not (source / ".question_run_id").exists()
    assert json.loads((source / "review_state.json").read_text()) == {
        "approved": True,
        "approved_by": "x",
        "reason": "needs_input",
        "qid": qid,
    }
    assert (
        json.loads((spec_dir / "implementation_plan.json").read_text())["reviewReason"]
        == "needs_input"
    )
    assert (
        json.loads((spec_dir / "implementation_plan.json").read_text())["phases"][0][
            "subtasks"
        ][0]["status"]
        == "pending"
    )


def test_failed_packed_transfer_never_publishes_pause(tmp_path, monkeypatch):
    spec_dir = tmp_path / "work" / "spec"
    _question(spec_dir)
    monkeypatch.setenv("WORKSPACE_URI", "s3://packed/work")
    monkeypatch.setattr(coder, "_checkpoint_question_branch", lambda *_: None)
    monkeypatch.setattr(workspace_fetch, "push_questions", lambda *_: False)
    status = _status()
    with pytest.raises(RuntimeError, match="question transfer failed"):
        coder._pause_for_question(tmp_path, spec_dir, None, status)
    assert status.states[-1] == BuildState.ERROR
    assert not (spec_dir / "review_state.json").exists()
    assert not (spec_dir / "implementation_plan.json").exists()


def test_old_question_does_not_trigger_pause(tmp_path):
    _question(tmp_path)
    start_question_run(tmp_path)
    assert coder._open_run_questions(tmp_path) == []


@pytest.mark.parametrize("failure", ["conflict", "sync"])
def test_local_pause_failure_rolls_back_created_questions(
    tmp_path, monkeypatch, failure
):
    spec_dir = tmp_path / "work" / "spec"
    source = tmp_path / "source"
    source.mkdir()
    _question(spec_dir)
    run_id = question_run_id(spec_dir)
    write_question(
        spec_dir, question="Another question?", run_id=run_id, asked_by_model="claude"
    )
    monkeypatch.setattr(coder, "_checkpoint_question_branch", lambda *_: None)
    monkeypatch.setattr(coder, "sync_plan_to_source", lambda *_: False)
    records = list_questions(spec_dir)
    existing = []
    if failure == "conflict":
        # The first record transfers before the second hits a conflicting id.
        existing = [{**records[-1], "question": "Existing question"}]
        assert create_question(source, existing[0])
    status = _status()
    message = "question already exists" if failure == "conflict" else "plan sync failed"
    with pytest.raises(RuntimeError, match=message):
        coder._pause_for_question(tmp_path, spec_dir, source, status)
    assert status.states[-1] == BuildState.ERROR
    assert list_questions(source) == existing
    assert not (source / "review_state.json").exists()


def test_create_question_accepts_identical_records_without_owning_them(tmp_path):
    _question(tmp_path)
    record = list_questions(tmp_path)[0]
    created = []
    assert create_question(tmp_path, record, created=created)
    assert not create_question(tmp_path, {**record, "question": "Different?"})
    assert created == []
    assert list((tmp_path / "questions").glob(f".{record['id']}*")) == []
    assert list_questions(tmp_path) == [record]


def test_parallel_merge_writes_the_question_record(tmp_path):
    _question(tmp_path / "worker")
    record = list_questions(tmp_path / "worker")[0]
    _write_merged_question(tmp_path / "parent", record)
    assert list_questions(tmp_path / "parent") == [record]


def test_packed_pause_pushes_question_before_status(tmp_path, monkeypatch):
    spec_dir = tmp_path / "spec"
    qid = _question(spec_dir)
    monkeypatch.setenv("WORKSPACE_URI", "s3://packed/work")
    calls = []
    monkeypatch.setattr(
        coder, "_checkpoint_question_branch", lambda *_: calls.append("checkpoint")
    )

    def transferred(*_args):
        assert not (spec_dir / "review_state.json").exists()
        calls.append("transfer")
        return True

    monkeypatch.setattr(workspace_fetch, "push_questions", transferred)
    status = _status()
    coder._pause_for_question(tmp_path, spec_dir, None, status)
    assert status.states[-1] == BuildState.PAUSED
    assert calls == ["checkpoint", "transfer"]
    assert json.loads((spec_dir / "review_state.json").read_text())["qid"] == qid


@pytest.mark.parametrize(
    "invalid", ["extra_key", "run_id", "status", "budget", "id", "size"]
)
def test_packed_question_transport_contains_only_records(
    tmp_path, monkeypatch, invalid
):
    spec_dir = tmp_path / "work"
    _question(spec_dir)
    old = list_questions(spec_dir)[0]
    qid = _question(spec_dir)
    run_id = question_run_id(spec_dir)
    answered = write_question(
        spec_dir, question="Already answered?", run_id=run_id, asked_by_model="claude"
    )
    update_question(
        spec_dir, answered["id"], expected_status="open", changes={"status": "answered"}
    )
    record = next(q for q in list_questions(spec_dir) if q["id"] == qid)
    blobs = {}

    class Store:
        def put_bytes(self, key, data, *_args, **_kwargs):
            blobs[key] = data

        def get_bytes(self, key, *, max_bytes):
            assert max_bytes == 64 * 1024
            return blobs[key]

    monkeypatch.setattr(artifact_store, "ArtifactStore", Store)
    assert workspace_fetch.push_questions(spec_dir, "001-x", run_id)
    assert len(blobs) == 1
    assert b".budget.json" not in next(iter(blobs.values()))
    before = set(tmp_path.rglob("*"))
    assert workspace_fetch.fetch_questions("001-x", run_id) == [record]
    invalid_records = {
        "extra_key": [{**record, "answer": "unexpected"}],
        "run_id": [old],
        "status": [{**record, "status": "answered"}],
        "budget": [record] * 4,
        "id": [{**record, "id": "../escape"}],
        "size": [{**record, "question": "x" * (64 * 1024)}],
    }
    key = next(iter(blobs))
    blobs[key] = json.dumps(invalid_records[invalid]).encode()
    with pytest.raises(ValueError, match="invalid question transfer"):
        workspace_fetch.fetch_questions("001-x", run_id)
    assert set(tmp_path.rglob("*")) == before


def _mock_checkpoint_git(monkeypatch, *, dirty=False, prestaged=False, push_rc=0):
    calls = []

    def fake_run(args, **_kwargs):
        calls.append(args)
        if args[1:3] == ["branch", "--show-current"]:
            return SimpleNamespace(returncode=0, stdout="aifactory/001-x\n", stderr="")
        if args[1:3] == ["remote", "get-url"]:
            return SimpleNamespace(
                returncode=0, stdout="https://example.invalid/repo", stderr=""
            )
        if args[1:3] == ["ls-files", "--others"]:
            return SimpleNamespace(
                returncode=0,
                stdout="code.py\0.env\0questions/q-123.json\0secrets.txt\0.aifactory/specs/x\0",
                stderr="",
            )
        if args[1:] == [
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--cached",
            "--quiet",
        ]:
            return SimpleNamespace(returncode=1 if dirty else 0, stdout="", stderr="")
        if prestaged and args[1:] == [
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--cached",
            "--name-only",
        ]:
            return SimpleNamespace(returncode=0, stdout=".env\n", stderr="")
        if args[1] == "push":
            return SimpleNamespace(returncode=push_rc, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    @contextmanager
    def fake_auth(url):
        yield url, {}

    monkeypatch.setattr(coder.subprocess, "run", fake_run)
    monkeypatch.setattr(coder, "authed_push_url", fake_auth)
    monkeypatch.setattr(coder, "lfs_upload", lambda *_: None)
    monkeypatch.setattr(coder, "scan_files", lambda *_: [])
    return calls


@pytest.mark.parametrize("dirty", [False, True])
def test_checkpoint_stages_only_code_and_handles_nothing_to_commit(
    tmp_path, monkeypatch, dirty
):
    calls = _mock_checkpoint_git(monkeypatch, dirty=dirty, prestaged=dirty)
    coder._checkpoint_question_branch(tmp_path, "001-x", "q-123456789abc")
    add_code = ["git", "add", "--", "code.py"]
    assert add_code in calls
    if dirty:
        assert calls[2] == ["git", "reset", "-q"]
        assert calls[3][:3] == ["git", "add", "-u"]
        assert calls.index(add_code) > 3
    else:
        assert ["git", "reset", "-q"] not in calls
    assert (
        ["git", "commit", "-m", "wip: paused for question q-123456789abc"] in calls
    ) == dirty
    assert (
        [
            "git",
            "commit",
            "--allow-empty",
            "-m",
            "wip: paused for question q-123456789abc",
        ]
        in calls
    ) != dirty
    assert any(args[1] == "push" for args in calls)


def test_git_push_failure_leaves_plan_and_review_state_unchanged(tmp_path, monkeypatch):
    spec_dir = tmp_path / "work" / "001-x"
    source = tmp_path / "source"
    source.mkdir()
    _question(spec_dir)
    plan = '{"status": "in_progress"}'
    review = '{"approved": true}'
    for directory in (spec_dir, source):
        (directory / "implementation_plan.json").write_text(plan)
        (directory / "review_state.json").write_text(review)
    calls = _mock_checkpoint_git(monkeypatch, push_rc=1)
    status = _status()
    with pytest.raises(RuntimeError, match="checkpoint push failed"):
        coder._pause_for_question(tmp_path, spec_dir, source, status)
    assert any(args[1] == "push" for args in calls)
    assert status.states[-1] == BuildState.ERROR
    for directory in (spec_dir, source):
        assert (directory / "implementation_plan.json").read_text() == plan
        assert (directory / "review_state.json").read_text() == review
    assert list_questions(source) == []


def test_checkpoint_refuses_detected_secret_before_push(tmp_path, monkeypatch):
    calls = []

    def fake_run(args, **_kwargs):
        calls.append(args)
        output = ""
        if args[1] == "branch":
            output = "aifactory/001-x"
        elif args[1:] == [
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--cached",
            "--name-only",
            "-z",
        ]:
            output = "code.py\0"
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    monkeypatch.setattr(coder.subprocess, "run", fake_run)
    monkeypatch.setattr(coder, "scan_files", lambda *_: [object()])
    with pytest.raises(RuntimeError, match="containing secrets"):
        coder._checkpoint_question_branch(tmp_path, "001-x", "q-123456789abc")
    assert not any(args[1] in {"commit", "push"} for args in calls)


@pytest.mark.parametrize("result", ["needs_input", None])
def test_build_command_returns_before_qa_and_finalize(tmp_path, monkeypatch, result):
    import agent
    import qa_loop
    from cli import build_commands

    spec_dir = tmp_path / "spec"
    spec_dir.mkdir()
    (spec_dir / "spec.md").write_text("test")

    async def pause(**_kwargs):
        if result is None:
            (spec_dir / "implementation_plan.json").write_text(
                json.dumps({"status": "human_review", "reviewReason": "needs_input"})
            )
        return result

    monkeypatch.setenv("AIFACTORY_AUTH_PREFLIGHT", "off")
    monkeypatch.setattr(agent, "run_autonomous_agent", pause)
    monkeypatch.setattr(build_commands, "_record_git_baseline_early", lambda *_: None)
    monkeypatch.setattr(
        build_commands.RunGates,
        "capture",
        lambda *_: SimpleNamespace(requires_review=False),
    )
    monkeypatch.setattr(
        build_commands,
        "ReviewState",
        SimpleNamespace(
            load=lambda *_: SimpleNamespace(
                is_approval_valid=lambda *_: True, approved_by="test"
            )
        ),
    )
    monkeypatch.setattr(build_commands, "get_existing_build_worktree", lambda *_: None)
    monkeypatch.setattr(
        build_commands,
        "choose_workspace",
        lambda *_a, **_kw: build_commands.WorkspaceMode.ISOLATED,
    )
    monkeypatch.setattr(
        build_commands,
        "setup_workspace",
        lambda *_a, **_kw: (tmp_path, object(), spec_dir),
    )
    monkeypatch.setattr(build_commands, "build_is_silent_noop", lambda *_: False)
    monkeypatch.setattr(build_commands, "handle_workspace_choice", MagicMock())
    monkeypatch.setattr("cli.utils.validate_environment", lambda *_: True)
    monkeypatch.setattr("cli.utils.print_banner", lambda: None)
    qa = MagicMock(return_value=False)
    finalize = MagicMock()
    monkeypatch.setattr(qa_loop, "should_run_qa", qa)
    monkeypatch.setattr(build_commands, "finalize_workspace", finalize)
    build_commands.handle_build_command(
        project_dir=tmp_path,
        spec_dir=spec_dir,
        model="claude-sonnet-4-5",
        max_iterations=1,
        verbose=False,
        force_isolated=True,
        force_direct=False,
        auto_continue=True,
        skip_qa=False,
        force_bypass_approval=False,
    )
    if result == "needs_input":
        qa.assert_not_called()
        finalize.assert_not_called()
    else:
        qa.assert_called_once()
        finalize.assert_called_once()


async def test_parallel_checkpoint_error_is_not_a_serial_fallback():
    async def failed_worker(_subtask, *, index):
        raise QuestionCheckpointError("checkpoint failed")

    async def merge(_subtask, _result):
        pytest.fail("failed worker must never merge")

    async def complete(_subtask):
        pytest.fail("failed worker must never complete")

    with pytest.raises(QuestionCheckpointError):
        await run_parallel_phase(
            [Subtask(id="1", description="test", files_to_create=["test.py"])],
            workers=1,
            run_subtask=failed_worker,
            merge_subtask=merge,
            mark_complete=complete,
        )


async def test_parallel_workers_raise_pause_after_all_question_merges():
    merged = []

    async def worker(subtask, *, index):
        return SubtaskResult(
            subtask_id=subtask.id,
            success=True,
            detail={"question": {"id": subtask.id}},
        )

    async def merge(subtask, _result):
        merged.append(subtask.id)
        return True

    async def complete(_subtask):
        pytest.fail("question worker must not complete its subtask")

    tasks = [
        Subtask(id=ident, description=ident, files_to_create=[f"{ident}.py"])
        for ident in ("1", "2")
    ]
    with pytest.raises(QuestionPause):
        await run_parallel_phase(
            tasks,
            workers=2,
            run_subtask=worker,
            merge_subtask=merge,
            mark_complete=complete,
        )
    assert merged == ["1", "2"]


@pytest.fixture
def coder_run(tmp_path, monkeypatch):
    spec_dir = tmp_path / "work" / "001-x"
    spec_dir.mkdir(parents=True)
    (spec_dir / "spec.md").write_text("# Feature\n")
    source = tmp_path / "source"
    source.mkdir()
    client = MagicMock()
    patchers = {
        "create_client": MagicMock(return_value=client),
        "run_session_guarded": AsyncMock(return_value=("complete", "", {})),
        "post_session_processing": AsyncMock(return_value=True),
        "check_git_metadata": MagicMock(),
        "_injection_scan_blocks": MagicMock(return_value=False),
        "_run_trailing_gates_if_build_complete": AsyncMock(),
        "_emit_build_report": MagicMock(),
        "get_graphiti_context": AsyncMock(return_value=""),
        "RecoveryManager": MagicMock(
            return_value=MagicMock(get_attempt_count=MagicMock(return_value=0))
        ),
        "StatusManager": MagicMock(),
        "emit_phase": MagicMock(),
        "get_task_logger": MagicMock(return_value=None),
        "debug_memory_system_status": MagicMock(),
        "record_turn": MagicMock(return_value={}),
        "charge_tokens": MagicMock(),
        "CompactionDetector": MagicMock(),
        "build_operational_context": MagicMock(return_value=""),
        "drain_inbox": MagicMock(return_value=[]),
        "is_first_run": MagicMock(return_value=False),
        "is_build_complete": MagicMock(return_value=False),
        "print_build_complete_banner": MagicMock(),
        "print_progress_summary": MagicMock(),
        "print_session_header": MagicMock(),
        "count_subtasks": MagicMock(return_value=(0, 1)),
        "count_subtasks_detailed": MagicMock(
            return_value={"completed": 0, "total": 1, "in_progress": 0}
        ),
        "get_next_subtask": MagicMock(return_value={"id": "1"}),
        "get_latest_commit": MagicMock(return_value="abc"),
        "get_commit_count": MagicMock(return_value=0),
        "generate_subtask_prompt": MagicMock(return_value="prompt"),
        "load_subtask_context": MagicMock(return_value={}),
    }
    monkeypatch.setenv("AIFACTORY_SOLO_MODE", "false")
    monkeypatch.setattr(
        "run_gates.get_phase_model", lambda *_a, **_kw: "claude-sonnet-4-5"
    )
    monkeypatch.setattr("run_gates.get_phase_thinking_budget", lambda *_a, **_kw: None)
    for name, replacement in patchers.items():
        monkeypatch.setattr(coder, name, replacement)

    async def run(**kwargs):
        return await coder.run_autonomous_agent(
            project_dir=spec_dir.parent,
            spec_dir=spec_dir,
            source_spec_dir=source,
            model="claude-sonnet-4-5",
            max_iterations=1,
            **kwargs,
        )

    return run, spec_dir, source


async def test_serial_coder_charges_asking_turn_and_pauses_before_processing(
    coder_run, monkeypatch
):
    run, spec_dir, source = coder_run
    usage = {"input_tokens": 25}

    async def ask(*_args, **_kwargs):
        write_question(
            spec_dir,
            question="Which format?",
            run_id=question_run_id(spec_dir),
            asked_by_model="claude",
        )
        return "continue", "", {"usage": usage}

    def pause(*_args):
        coder.charge_tokens.assert_called_once_with(spec_dir, usage)
        coder.record_turn.assert_called_once()

    pause = MagicMock(side_effect=pause)
    monkeypatch.setattr(coder, "run_session_guarded", ask)
    monkeypatch.setattr(coder, "_pause_for_question", pause)
    assert await run() == "needs_input"
    pause.assert_called_once_with(
        spec_dir.parent, spec_dir, source, coder.StatusManager.return_value
    )
    coder.post_session_processing.assert_not_called()


@pytest.mark.parametrize("packed", [False, True])
async def test_coder_reuses_question_run_only_when_requested(
    coder_run, monkeypatch, packed
):
    run, spec_dir, source = coder_run
    _question(source)
    _question(source)
    if packed:
        monkeypatch.setenv("WORKSPACE_URI", "s3://packed/work")
    existing = start_question_run(spec_dir)
    assert await run(reuse_question_run=True) is None
    assert question_run_id(spec_dir) == existing
    assert await run() is None
    assert question_run_id(spec_dir) != existing
    counts = [
        call.kwargs["prior_task_count"] for call in coder.create_client.call_args_list
    ]
    assert counts == [
        None if packed else 2,
        None if packed else 2,
    ]


@pytest.mark.parametrize("metadata_fails", [False, True])
async def test_parallel_coder_detects_shared_question_without_pause_exception(
    coder_run, monkeypatch, metadata_fails
):
    run, spec_dir, _source = coder_run

    async def failed_worker(**_kwargs):
        write_question(
            spec_dir,
            question="Which format?",
            run_id=question_run_id(spec_dir),
            asked_by_model="claude",
        )
        return False

    monkeypatch.setattr(coder, "_maybe_run_parallel_phase", failed_worker)
    pause = MagicMock()
    monkeypatch.setattr(coder, "_pause_for_question", pause)
    if metadata_fails:
        coder.check_git_metadata.side_effect = RuntimeError("metadata changed")
        with pytest.raises(RuntimeError, match="metadata changed"):
            await run(parallel=True)
        assert (
            coder.StatusManager.return_value.update.call_args.kwargs["state"]
            == BuildState.ERROR
        )
        pause.assert_not_called()
    else:
        assert await run(parallel=True) == "needs_input"
        coder.check_git_metadata.assert_called_once_with(spec_dir.parent)
        pause.assert_called_once()
    coder.run_session_guarded.assert_not_called()
