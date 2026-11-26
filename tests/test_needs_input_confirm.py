"""Control-plane confirmation of a runner's needs-input pause."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from agents.questions import RUN_ID_RE, TASK_QUESTION_LIMIT, create_question
from core import artifact_store, workspace_fetch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "web-server"))

from server import project_registry  # noqa: E402
from server.rmux import integration as rmux  # noqa: E402
from server.services import (  # noqa: E402
    agent_kubejob,
    agent_service,
    build_backend,
    completion,
    completion_orchestration,
    job_s3_scope,
    project_settings_store,
    question_pause,
    task_control,
    trusted_contract,
    trusted_contract_store,
)
from server.specpath import spec_dir_for  # noqa: E402

SPEC = "001-test"
TASK = f"project:{SPEC}"
ATTEMPT_ENV = "AIFACTORY_ATTEMPT_ID"
COUNT_ENV = "AIFACTORY_TASK_QUESTION_COUNT"
ATTEMPT_FILE = ".question_attempt"
OLD_ATTEMPT = "0" * 32


@pytest.fixture
def spec(tmp_path):
    directory = spec_dir_for(tmp_path, SPEC)
    directory.mkdir(parents=True)
    return directory


@pytest.fixture
def blobs(monkeypatch):
    data = {}

    class Store:
        def get_bytes(self, key, **_kwargs):
            return data[key]

    monkeypatch.setattr(artifact_store, "ArtifactStore", Store)
    return data


def _record(attempt, number=0):
    return {
        "id": f"q-{number:012x}",
        "run_id": attempt,
        "status": "open",
        "question": "Which output format should the build use?",
        "choices": ["JSON", "CSV"],
        "context": "",
        "default": None,
        "needs_approval": False,
        "timeout_minutes": 30,
        "asked_at": "2026-11-26T12:00:00Z",
        "asked_by_model": "claude",
    }


def _seed_questions(spec, count):
    directory = spec / "questions"
    directory.mkdir(exist_ok=True)
    for number in range(1, count + 1):
        (directory / f"q-{number:012x}.json").write_text("malformed")


def _plan(spec, **state):
    path = spec / "implementation_plan.json"
    path.write_text(
        json.dumps({"phases": [{"name": "Build", "subtasks": []}], **state})
    )
    return path


def test_start_attempt_counts_names_without_parsing(spec):
    _seed_questions(spec, 2)
    for name in (".q-000000000003.json", "q-other.json", "q-000000000004.txt"):
        (spec / "questions" / name).write_text("ignored")
    (spec / "questions" / "q-000000000005.json").mkdir()
    first = question_pause.start_attempt(spec)
    assert RUN_ID_RE.fullmatch(first[ATTEMPT_ENV])
    assert (spec / ATTEMPT_FILE).read_text() == first[ATTEMPT_ENV]
    assert first[COUNT_ENV] == "2"

    second = question_pause.start_attempt(spec)
    assert (spec / ATTEMPT_FILE).read_text() == second[ATTEMPT_ENV]
    assert first[ATTEMPT_ENV] != second[ATTEMPT_ENV]


def test_dispatch_without_source_cannot_confirm_a_pause(tmp_path, blobs):
    source = tmp_path / "missing"
    env = question_pause.start_attempt(source)
    attempt = env[ATTEMPT_ENV]
    blobs[workspace_fetch._questions_key(SPEC, attempt)] = json.dumps(
        [_record(attempt)]
    ).encode()

    assert env[COUNT_ENV] == "0"
    assert question_pause.confirm_kubejob(source, SPEC) is None
    assert not source.exists()


def test_kubejob_confirmation_is_idempotent(spec, blobs):
    _seed_questions(spec, TASK_QUESTION_LIMIT - 1)
    attempt = question_pause.start_attempt(spec)[ATTEMPT_ENV]
    record = _record(attempt)
    blobs[workspace_fetch._questions_key(SPEC, attempt)] = json.dumps([record]).encode()

    assert question_pause.confirm_kubejob(spec, SPEC) == record
    path = spec / "questions" / f"{record['id']}.json"
    assert json.loads(path.read_text()) == record
    before = path.stat()
    assert question_pause.confirm_kubejob(spec, SPEC) == record
    assert (path.stat().st_ino, path.stat().st_mtime_ns) == (
        before.st_ino,
        before.st_mtime_ns,
    )
    assert len(list(path.parent.iterdir())) == TASK_QUESTION_LIMIT


@pytest.mark.parametrize(
    "problem",
    [
        "missing",
        "earlier",
        "run_id",
        "extra_key",
        "closed",
        "budget",
        "conflict",
        "no_attempt",
        "bad_attempt",
    ],
)
def test_kubejob_refuses_without_leaving_new_records(spec, blobs, problem):
    attempt = question_pause.start_attempt(spec)[ATTEMPT_ENV]
    record = _record(attempt)
    records = [record]
    key = workspace_fetch._questions_key(SPEC, attempt)
    if problem == "earlier":
        key = workspace_fetch._questions_key(SPEC, OLD_ATTEMPT)
        record["run_id"] = OLD_ATTEMPT
    elif problem == "run_id":
        records.append(_record(OLD_ATTEMPT, 1))
    elif problem == "extra_key":
        records.append({**_record(attempt, 1), "answer": "unexpected"})
    elif problem == "closed":
        record["status"] = "answered"
    elif problem == "budget":
        _seed_questions(spec, TASK_QUESTION_LIMIT)
    elif problem == "conflict":
        conflict = _record(attempt, 1)
        assert create_question(spec, {**conflict, "question": "Already stored"})
        records.append(conflict)
    elif problem == "no_attempt":
        (spec / ATTEMPT_FILE).unlink()
    elif problem == "bad_attempt":
        (spec / ATTEMPT_FILE).write_text(f"{attempt}\n")
    if problem != "missing":
        blobs[key] = json.dumps(records).encode()

    before = {path: path.read_bytes() for path in spec.rglob("*") if path.is_file()}
    assert question_pause.confirm_kubejob(spec, SPEC) is None
    assert {
        path: path.read_bytes() for path in spec.rglob("*") if path.is_file()
    } == before


@pytest.fixture
def stub_spawn_services(monkeypatch):
    monkeypatch.setattr(trusted_contract_store, "stamp_spawn", AsyncMock())
    monkeypatch.setattr(trusted_contract_store, "lookup", AsyncMock(return_value=None))
    monkeypatch.setattr(trusted_contract, "spawn_env", AsyncMock(return_value={}))
    monkeypatch.setattr(
        project_settings_store, "mcp_spawn_env", AsyncMock(return_value={})
    )


@pytest.mark.usefixtures("stub_spawn_services")
@pytest.mark.asyncio
async def test_kubejob_dispatch_passes_attempt_and_prior_count(
    tmp_path, spec, monkeypatch
):
    _seed_questions(spec, 2)
    monkeypatch.setattr(
        build_backend, "populate_build_worktree", Mock(return_value=tmp_path)
    )
    monkeypatch.setattr(
        build_backend,
        "_maybe_pack_workspace",
        Mock(return_value="s3://packed/workspace"),
    )
    monkeypatch.setattr(build_backend, "build_job_env", Mock(return_value={}))
    monkeypatch.setattr(job_s3_scope, "scope_job_s3", Mock())
    batch = SimpleNamespace(create_namespaced_job=AsyncMock())
    store = SimpleNamespace(set_worker_ref=AsyncMock())

    await build_backend.KubeJobBuildBackend(store).dispatch(
        task_id=TASK, project_path=tmp_path, spec_id=SPEC, batch=batch
    )

    manifest = batch.create_namespaced_job.await_args.args[1]
    env = manifest["spec"]["template"]["spec"]["containers"][0]["env"]
    values = {entry["name"]: entry.get("value") for entry in env}
    assert values[ATTEMPT_ENV] == (spec / ATTEMPT_FILE).read_text()
    assert values[COUNT_ENV] == "2"


@pytest.mark.usefixtures("stub_spawn_services")
@pytest.mark.asyncio
async def test_subprocess_spawn_passes_only_attempt_id(tmp_path, spec, monkeypatch):
    service = agent_service.AgentService()
    service.settings = SimpleNamespace(BACKEND_PATH=str(tmp_path / "backend"))
    monkeypatch.setattr(
        service, "_resolve_claude_token_pooled", Mock(return_value=(None, None, None))
    )
    monkeypatch.setattr(service, "_emit_progress", AsyncMock())
    monkeypatch.setattr(agent_service, "make_subprocess_env", Mock(return_value={}))
    monkeypatch.setattr(agent_service, "spawn", lambda coroutine: coroutine.close())
    monkeypatch.setattr(project_registry, "load_projects", Mock(return_value={}))
    monkeypatch.setattr(project_settings_store, "load_project_env", AsyncMock())
    monkeypatch.setattr(rmux, "create_if_enabled", AsyncMock())
    monkeypatch.delenv("AIFACTORY_TEST_AGENT_CMD", raising=False)
    monkeypatch.setattr("pty.openpty", Mock(return_value=(10, 11)))
    monkeypatch.setattr(agent_service.os, "close", Mock())
    spawn_process = AsyncMock(return_value=SimpleNamespace(stdout=None, stderr=None))
    monkeypatch.setattr(agent_service.asyncio, "create_subprocess_exec", spawn_process)

    await service._spawn_task_execution(TASK, tmp_path, SPEC)

    env = spawn_process.await_args.kwargs["env"]
    assert RUN_ID_RE.fullmatch(env[ATTEMPT_ENV])
    assert env[ATTEMPT_ENV] == (spec / ATTEMPT_FILE).read_text()
    assert COUNT_ENV not in env


@pytest.mark.parametrize("confirmed", [True, False])
@pytest.mark.asyncio
async def test_kubejob_done_only_pauses_with_a_question(
    tmp_path, spec, blobs, monkeypatch, confirmed
):
    attempt = question_pause.start_attempt(spec)[ATTEMPT_ENV]
    _plan(spec, status="human_review", reviewReason="needs_input")
    if confirmed:
        blobs[workspace_fetch._questions_key(SPEC, attempt)] = json.dumps(
            [_record(attempt)]
        ).encode()
    monkeypatch.setattr(
        agent_kubejob, "resolve_project_path", Mock(return_value=tmp_path)
    )
    monkeypatch.setattr(agent_kubejob, "_report_orphaned_worktrees", AsyncMock())
    opened = Mock()
    monkeypatch.setattr(question_pause, "on_question_open", opened)
    terminal = AsyncMock(return_value="completed")
    service = SimpleNamespace(
        _release_task_credential=Mock(),
        _safe_emit_task_status=AsyncMock(),
        _active_job_ids=AsyncMock(),
        _drain_queue=AsyncMock(),
        _report_kubejob_usage=AsyncMock(),
        _emit_kubejob_terminal_completion=terminal,
        _record_kubejob_terminal=AsyncMock(),
    )

    await agent_kubejob.KubejobMixin._on_kubejob_build_done(service, TASK)

    if confirmed:
        control = task_control.read_control(spec)
        assert (control["status"], control["reviewReason"]) == (
            "human_review",
            "needs_input",
        )
        service._safe_emit_task_status.assert_awaited_once_with(
            TASK, "human_review", "needs_input"
        )
        opened.assert_called_once_with(TASK)
        terminal.assert_not_awaited()
        service._record_kubejob_terminal.assert_not_awaited()
    else:
        terminal.assert_awaited_once_with(TASK)
        service._record_kubejob_terminal.assert_awaited_once_with(TASK, "completed")
        service._safe_emit_task_status.assert_not_awaited()
        opened.assert_not_called()


@pytest.mark.parametrize(
    "case", ["confirmed", "earlier", "over_budget", "review_only", "plan_review"]
)
@pytest.mark.asyncio
async def test_local_monitor_handles_needs_input_and_plan_review(
    tmp_path, spec, monkeypatch, case
):
    attempt = question_pause.start_attempt(spec)[ATTEMPT_ENV]
    record = _record(
        OLD_ATTEMPT if case == "earlier" else attempt, TASK_QUESTION_LIMIT + 1
    )
    _seed_questions(spec, TASK_QUESTION_LIMIT if case == "over_budget" else 1)
    assert create_question(spec, record)
    review = {"reason": "needs_input", "qid": record["id"]}
    if case == "plan_review":
        review = {"approved": False}
    (spec / "review_state.json").write_text(json.dumps(review))
    if case in {"plan_review", "review_only"}:
        _plan(spec, status="in_progress")
    elif case == "confirmed":
        _plan(
            spec,
            status="human_review",
            reviewReason="needs_input",
            phases=[{"name": "Build", "subtasks": [{"status": "pending"}]}],
        )
    else:
        _plan(spec, status="human_review", reviewReason="needs_input")

    service = agent_service.AgentService()
    service._store_enabled = True
    proc = SimpleNamespace(wait=AsyncMock(return_value=0))
    service.running_tasks[TASK] = proc
    service._spec_dirs[TASK] = spec
    writers = (Mock(), Mock())
    service._task_log_writers[TASK] = writers
    store = SimpleNamespace(
        get_state=AsyncMock(return_value=None), mark_terminal=AsyncMock()
    )
    update = AsyncMock()
    emit = AsyncMock()
    progress = AsyncMock()
    opened = Mock()
    release = Mock()
    monkeypatch.setattr(service, "_store", Mock(return_value=store))
    monkeypatch.setattr(service, "_sync_worktree_files", AsyncMock())
    monkeypatch.setattr(service, "_update_plan_status", update)
    monkeypatch.setattr(service, "_safe_emit_task_status", emit)
    monkeypatch.setattr(service, "_emit_progress", progress)
    monkeypatch.setattr(service, "_release_task_credential", release)
    monkeypatch.setattr(service, "_drain_queue", AsyncMock())
    monkeypatch.setattr(service, "start_task_execution", AsyncMock())
    monkeypatch.setattr(question_pause, "on_question_open", opened)
    monkeypatch.setattr(completion, "emit_usage_snapshot", Mock())
    monkeypatch.setattr(rmux, "reap_if_enabled", AsyncMock())

    await service._monitor_process(
        TASK, proc, tmp_path, SPEC, ["run.py"], {"CI": "true"}
    )

    assert TASK not in service.running_tasks
    assert TASK not in service._spec_dirs
    assert TASK not in service._task_log_writers
    release.assert_called_once_with(TASK)
    for writer in writers:
        writer.finalize.assert_called_once()
    if case == "confirmed":
        control = task_control.read_control(spec)
        assert (control["status"], control["reviewReason"], control["updatedBy"]) == (
            "human_review",
            "needs_input",
            "needs_input_pause",
        )
        emit.assert_awaited_once_with(TASK, "human_review", "needs_input")
        opened.assert_called_once_with(TASK)
        update.assert_not_awaited()
        progress.assert_not_awaited()
        service.start_task_execution.assert_not_awaited()
        store.mark_terminal.assert_awaited_once_with(TASK, "review", error=None)
    elif case == "plan_review":
        update.assert_awaited_once_with(tmp_path, SPEC, "human_review", TASK)
        opened.assert_not_called()
    else:
        update.assert_awaited_once_with(
            tmp_path, SPEC, "completed", TASK, emit_events=False
        )
        emit.assert_not_awaited()
        opened.assert_not_called()


@pytest.mark.parametrize("terminal", ["completed", "failed"])
@pytest.mark.asyncio
async def test_plan_status_preserves_needs_input_until_terminal(
    tmp_path, spec, monkeypatch, terminal
):
    plan = _plan(spec, status="human_review", reviewReason="needs_input")
    task_control.record_plan_approval(spec, "reviewer")
    task_control.write_control(spec, status="human_review", review_reason="needs_input")
    control = task_control.write_control(spec, status="human_review")
    assert control["reviewReason"] == "needs_input"
    assert control["planApproved"] is True
    original = plan.read_bytes()
    service = agent_service.AgentService()
    completion_call = AsyncMock()
    monkeypatch.setattr(
        completion_orchestration, "run_terminal_completion", completion_call
    )

    await service._update_plan_status(
        tmp_path, SPEC, "human_review", TASK, emit_events=False
    )

    assert plan.read_bytes() == original
    assert task_control.read_control(spec) == control
    completion_call.assert_not_awaited()

    await service._update_plan_status(tmp_path, SPEC, terminal, TASK, emit_events=False)

    assert json.loads(plan.read_text())["reviewReason"] != "needs_input"
    assert task_control.read_control(spec)["reviewReason"] != "needs_input"
    completion_call.assert_awaited_once()
