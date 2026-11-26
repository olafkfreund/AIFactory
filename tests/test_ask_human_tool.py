"""Coder-only MCP registration and tool behavior."""

import asyncio
from types import SimpleNamespace

import pytest
from agents.parallel_integration import _seed_child_spec
from agents.questions import list_questions, start_question_run
from agents.tools_pkg import get_allowed_tools
from agents.tools_pkg.registry import create_all_tools, create_magestic_ai_mcp_server
from agents.tools_pkg.tools.human import create_human_tools


def _sdk_tool(name, description, schema):
    return lambda handler: SimpleNamespace(
        name=name, handler=handler, input_schema=schema
    )


def test_coder_only_registration(tmp_path, monkeypatch):
    from agents.tools_pkg import registry
    from agents.tools_pkg.tools import human

    monkeypatch.setattr(human, "tool", _sdk_tool)
    captured = []
    monkeypatch.setattr(
        registry,
        "create_sdk_mcp_server",
        lambda **kwargs: captured.append(kwargs["tools"]) or kwargs,
    )
    start_question_run(tmp_path)
    assert "mcp__aifactory__ask_human" in get_allowed_tools("coder")
    assert "mcp__aifactory__ask_human" not in get_allowed_tools("planner")
    assert "ask_human" not in [
        tool.name for tool in create_all_tools(tmp_path, tmp_path)
    ]
    create_magestic_ai_mcp_server(tmp_path, tmp_path)
    assert "ask_human" not in [tool.name for tool in captured[-1]]
    create_magestic_ai_mcp_server(tmp_path, tmp_path, coder_model="claude")
    assert "ask_human" in [tool.name for tool in captured[-1]]


@pytest.mark.parametrize(
    "agent_type,expected_model", [("coder", "claude"), ("planner", None)]
)
def test_create_client_passes_coder_model(
    tmp_path, monkeypatch, agent_type, expected_model
):
    from unittest.mock import MagicMock

    from core import client

    captured = []
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(client, "require_auth_token", lambda: "test-token")
    monkeypatch.setattr(client, "is_tools_available", lambda: True)
    monkeypatch.setattr(
        client, "get_required_mcp_servers", lambda *_a, **_kw: ["aifactory"]
    )
    monkeypatch.setattr(
        client,
        "create_magestic_ai_mcp_server",
        lambda *_a, **kw: captured.append(kw) or {},
    )
    monkeypatch.setattr(client, "ClaudeSDKClient", lambda **_kw: MagicMock())
    budget_dir = tmp_path / "budget"
    client.create_client(
        tmp_path,
        tmp_path,
        "claude",
        agent_type=agent_type,
        question_budget_spec_dir=budget_dir,
    )
    assert captured[0]["coder_model"] == expected_model
    assert captured[0]["question_budget_spec_dir"] == budget_dir


def test_tool_writes_question_and_returns_stop_message(tmp_path, monkeypatch):
    from agents.tools_pkg.tools import human

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
    from agents.tools_pkg.tools import human

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
    from agents.tools_pkg.tools import human

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
    from agents.tools_pkg.tools import human

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
    from agents.tools_pkg.tools import human

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
    from agents.tools_pkg.tools import human

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
    from agents.tools_pkg.tools import human

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
