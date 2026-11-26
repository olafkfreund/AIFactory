"""Coder-only MCP registration and tool behavior."""

import asyncio
from types import SimpleNamespace

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
