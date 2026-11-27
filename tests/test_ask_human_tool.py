"""Coder-only MCP registration and tool behavior."""

import asyncio
import importlib
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import claude_agent_sdk
import pytest
from agents import coder, parallel_integration
from agents.parallel_integration import _seed_child_spec
from agents.parallel_runner import PhaseRunResult
from agents.questions import list_questions, start_question_run
from agents.tools_pkg import get_allowed_tools, registry
from agents.tools_pkg.models import TOOL_ASK_HUMAN
from agents.tools_pkg.registry import create_all_tools, create_magestic_ai_mcp_server
from agents.tools_pkg.tools import human
from agents.tools_pkg.tools.human import create_human_tools
from core import client
from implementation_plan.plan import ImplementationPlan
from implementation_plan.subtask import Subtask
from mcp_server import aifactory_server
from prompts_pkg.prompt_generator import generate_subtask_prompt
from run_gates import RunGates


def _sdk_tool(name, _description, schema):
    return lambda handler: SimpleNamespace(
        name=name, handler=handler, input_schema=schema
    )


def test_coder_only_registration(tmp_path, monkeypatch):
    monkeypatch.setattr(human, "tool", _sdk_tool)
    monkeypatch.setattr(registry, "create_sdk_mcp_server", lambda **kwargs: kwargs)
    start_question_run(tmp_path)
    assert TOOL_ASK_HUMAN not in get_allowed_tools("coder")
    assert TOOL_ASK_HUMAN not in get_allowed_tools("planner")
    assert "ask_human" not in [
        tool.name for tool in create_all_tools(tmp_path, tmp_path)
    ]
    common = create_magestic_ai_mcp_server(tmp_path, tmp_path)
    assert "ask_human" not in [tool.name for tool in common["tools"]]
    coder_server = create_magestic_ai_mcp_server(
        tmp_path, tmp_path, coder_model="claude"
    )
    assert "ask_human" in [tool.name for tool in coder_server["tools"]]


async def test_standalone_omits_ask_human(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class ServerBuiltError(Exception):
        pass

    def capture(*, tools: list[Any], **_kwargs: object) -> None:
        assert all(tool.name != "ask_human" for tool in tools)
        raise ServerBuiltError

    monkeypatch.setattr(claude_agent_sdk, "create_sdk_mcp_server", capture)
    monkeypatch.setattr(aifactory_server, "_control_tools", lambda: [])
    with pytest.raises(ServerBuiltError):
        await aifactory_server._run(lambda: tmp_path)


@pytest.fixture
def sdk_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    sdk = MagicMock()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(client, "require_auth_token", lambda: "test-token")
    monkeypatch.setattr(human, "tool", _sdk_tool)
    monkeypatch.setattr(
        client, "get_required_mcp_servers", lambda *_a, **_kw: ["aifactory"]
    )
    monkeypatch.setattr(registry, "create_sdk_mcp_server", lambda **kwargs: kwargs)
    monkeypatch.setattr(client, "ClaudeSDKClient", sdk)
    return sdk


def _assert_registration(sdk: MagicMock, expected: bool) -> None:
    options = sdk.call_args.kwargs["options"]
    assert (TOOL_ASK_HUMAN in options.allowed_tools) is expected
    tools = options.mcp_servers["aifactory"]["tools"]
    assert any(tool.name == "ask_human" for tool in tools) is expected


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("local", True),
        ("packed", True),
        ("co-mounted", False),
        ("direct", False),
        ("no origin", False),
        ("git error", False),
        ("planner", False),
    ],
)
def test_serial_registration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sdk_client: MagicMock,
    case: str,
    expected: bool,
) -> None:
    for name in ("JOB_ID", "FACTORY_SERVICE", "WORKSPACE_URI"):
        monkeypatch.delenv(name, raising=False)
    if case in {"packed", "co-mounted"}:
        monkeypatch.setenv("JOB_ID", "project:001-x")
        monkeypatch.setenv("FACTORY_SERVICE", "aifactory")
    if case == "packed":
        monkeypatch.setenv("WORKSPACE_URI", "s3://packed/work")

    def git(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if case == "git error" or (case == "no origin" and args[1] == "remote"):
            raise subprocess.CalledProcessError(1, args)
        if args[1] == "branch":
            branch = "main" if case == "direct" else "aifactory/001-x"
            return subprocess.CompletedProcess(args, 0, f"{branch}\n")
        assert args == ["git", "remote", "get-url", "origin"]
        return subprocess.CompletedProcess(args, 0, "/local/origin.git\n")

    with monkeypatch.context() as patch:
        patch.setattr(coder.subprocess, "run", git)
        enabled = coder._can_ask_human(tmp_path, "001-x")
    agent_type = "planner" if case == "planner" else "coder"
    client.create_client(
        tmp_path,
        tmp_path,
        "claude",
        agent_type=agent_type,
        ask_human_enabled=enabled,
    )
    _assert_registration(sdk_client, expected)
    prompt = generate_subtask_prompt(
        tmp_path, tmp_path, {}, {}, ask_human_enabled=enabled and agent_type == "coder"
    )
    assert ("`ask_human`" in prompt) is expected


@pytest.mark.parametrize("enabled", [True, False])
async def test_parallel_worker_inherits_registration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sdk_client: MagicMock,
    enabled: bool,
) -> None:
    spec_dir = tmp_path / "001-x"
    spec_dir.mkdir()
    child = tmp_path / "worker"
    child.mkdir()
    manager = MagicMock()
    manager.create_worktree.return_value = SimpleNamespace(path=child)
    monkeypatch.setattr(
        importlib.import_module("core.worktree"),
        "WorktreeManager",
        MagicMock(return_value=manager),
    )
    session = AsyncMock(return_value=("complete", "", {}))
    monkeypatch.setattr(parallel_integration, "run_session_guarded", session)
    monkeypatch.setattr(
        parallel_integration, "get_graphiti_context", AsyncMock(return_value="")
    )
    monkeypatch.setattr(parallel_integration, "_capture_memory", AsyncMock())
    monkeypatch.setattr(parallel_integration, "check_git_metadata", MagicMock())
    monkeypatch.setattr(parallel_integration, "record_identity", MagicMock())
    gates = RunGates.capture(spec_dir, None, "claude-sonnet-4-5")
    phase = SimpleNamespace(
        name="Build", subtasks=[Subtask(id="1", description="Build")]
    )

    async def run_wave(subtasks: list[Subtask], **kwargs: Any) -> PhaseRunResult:
        await kwargs["run_subtask"](subtasks[0], index=0)
        return PhaseRunResult()

    monkeypatch.setattr(parallel_integration, "run_parallel_phase", run_wave)
    await parallel_integration.run_parallel_coding_phase(
        plan=MagicMock(spec=ImplementationPlan),
        phase=phase,
        project_dir=tmp_path,
        spec_dir=spec_dir,
        model="claude-sonnet-4-5",
        workers=1,
        gates=gates,
        ask_human_enabled=enabled,
    )

    _assert_registration(sdk_client, enabled)
    call = session.await_args
    assert call is not None
    assert ("`ask_human`" in call.args[1]) is enabled


def test_tool_writes_question_and_returns_stop_message(tmp_path, monkeypatch):
    monkeypatch.setattr(human, "tool", _sdk_tool)
    start_question_run(tmp_path)
    tool = create_human_tools(tmp_path, "claude")[0]
    result = asyncio.run(
        tool.handler({"question": "Which format?", "choices": ["JSON", "CSV"]})
    )
    assert "Stop now" in result["content"][0]["text"]
    assert len(list_questions(tmp_path)) == 1
    assert list_questions(tmp_path)[0]["asked_by_model"] == "claude"


def test_tool_schema_has_limits(tmp_path, monkeypatch):
    monkeypatch.setattr(human, "tool", _sdk_tool)
    schema = create_human_tools(tmp_path, "claude")[0].input_schema["properties"]
    assert schema["question"]["maxLength"] == 2000
    assert schema["context"]["maxLength"] == 4000
    assert schema["default"]["maxLength"] == 500
    assert schema["choices"]["items"]["maxLength"] == 200
    assert (schema["choices"]["minItems"], schema["choices"]["maxItems"]) == (2, 6)
    assert (
        schema["timeout_minutes"]["minimum"],
        schema["timeout_minutes"]["maximum"],
    ) == (1, 1440)


def test_task_budget_from_env_on_each_call(tmp_path, monkeypatch):
    monkeypatch.setattr(human, "tool", _sdk_tool)
    monkeypatch.delenv("AIFACTORY_TASK_QUESTION_COUNT", raising=False)
    start_question_run(tmp_path)
    tool = create_human_tools(tmp_path, "claude")[0]
    monkeypatch.setenv("AIFACTORY_TASK_QUESTION_COUNT", "9")
    first = asyncio.run(tool.handler({"question": "First?"}))
    second = asyncio.run(tool.handler({"question": "Second?"}))
    assert "Stop now" in first["content"][0]["text"]
    assert (
        second["content"][0]["text"]
        == "question budget exhausted: proceed on your best reading or stop"
    )
    assert len(list_questions(tmp_path)) == 1


@pytest.mark.parametrize("value", ["10", "abc", "-1", "9" * 5000])
def test_env_exhaustion_refuses_first_question(tmp_path, monkeypatch, value):
    monkeypatch.setattr(human, "tool", _sdk_tool)
    monkeypatch.setenv("AIFACTORY_TASK_QUESTION_COUNT", value)
    start_question_run(tmp_path)
    result = asyncio.run(
        create_human_tools(tmp_path, "claude")[0].handler({"question": "First?"})
    )
    assert result["isError"] is True
    assert (
        result["content"][0]["text"]
        == "question budget exhausted: proceed on your best reading or stop"
    )
    assert list_questions(tmp_path) == []


@pytest.mark.parametrize("env_value", [None, ""])
def test_unset_or_empty_env_and_explicit_count(tmp_path, monkeypatch, env_value):
    monkeypatch.setattr(human, "tool", _sdk_tool)
    if env_value is None:
        monkeypatch.delenv("AIFACTORY_TASK_QUESTION_COUNT", raising=False)
    else:
        monkeypatch.setenv("AIFACTORY_TASK_QUESTION_COUNT", env_value)
    start_question_run(tmp_path)
    assert (
        "Stop now"
        in asyncio.run(
            create_human_tools(tmp_path, "claude")[0].handler({"question": "First?"})
        )["content"][0]["text"]
    )
    monkeypatch.setenv("AIFACTORY_TASK_QUESTION_COUNT", "abc")
    tool = create_human_tools(tmp_path, "claude", prior_task_count=0)[0]
    assert (
        "Stop now"
        in asyncio.run(tool.handler({"question": "Second?"}))["content"][0]["text"]
    )


def test_tool_reports_budget_error(tmp_path, monkeypatch):
    monkeypatch.setattr(human, "tool", _sdk_tool)
    start_question_run(tmp_path)
    tool = create_human_tools(tmp_path, "claude")[0]
    for _ in range(3):
        asyncio.run(tool.handler({"question": "Still unclear?"}))
    result = asyncio.run(tool.handler({"question": "One more?"}))
    assert result["isError"] is True
    assert result["content"][0]["text"] == (
        "question budget exhausted: proceed on your best reading or stop"
    )
    assert len(list_questions(tmp_path)) == 3


def test_parallel_worker_keeps_run_id_and_uses_parent_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(human, "tool", _sdk_tool)
    parent = tmp_path / "parent"
    child = tmp_path / "worker"
    parent.mkdir()
    (parent / "spec.md").write_text("spec")
    run_id = start_question_run(parent)
    assert _seed_child_spec(parent, child) == child
    tool = create_human_tools(child, "claude", budget_spec_dir=parent)[0]
    asyncio.run(tool.handler({"question": "Which format?"}))
    assert list_questions(child)[0]["run_id"] == run_id
    assert list_questions(parent) == []
