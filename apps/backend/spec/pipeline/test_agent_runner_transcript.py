"""Spec pipeline tool calls appear in the run transcript."""

import json

import pytest
from task_logger import TaskLogger, transcript

from . import agent_runner


@pytest.mark.asyncio
async def test_tool_events_are_written(tmp_path, monkeypatch):
    class ToolUseBlock:
        name = "Read"
        id = "tool-1"
        input = {"file_path": "example.txt"}

    class ToolResultBlock:
        tool_use_id = "tool-1"
        content = "example content"
        is_error = False

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def query(self, _prompt):
            return None

        async def receive_response(self):
            yield AssistantMessage()
            yield UserMessage()

    # The runner checks message class names supplied by the SDK.
    class AssistantMessage:
        content = [ToolUseBlock()]

    class UserMessage:
        content = [ToolResultBlock()]

    monkeypatch.setattr(agent_runner, "create_client", lambda *_a, **_kw: Client())
    transcript.start(tmp_path)
    try:
        runner = agent_runner.AgentRunner(
            tmp_path,
            tmp_path,
            "claude-sonnet-4-5",
            TaskLogger(tmp_path, emit_markers=False),
        )
        success, _ = await runner.run_agent("spec_gatherer.md")
        assert success
        rows = [
            json.loads(line)
            for line in transcript.written_files()[0].read_text().splitlines()
        ]
        assert [(row["kind"], row["tool"], row["tool_use_id"]) for row in rows] == [
            ("tool_start", "Read", "tool-1"),
            ("tool_end", "Read", "tool-1"),
        ]
        assert rows[0]["payload"] == {"file_path": "example.txt"}
        assert rows[1]["payload"] == "example content"
    finally:
        transcript.reset()


@pytest.mark.asyncio
async def test_every_result_of_a_multi_tool_message_is_written(tmp_path, monkeypatch):
    def use(i):
        return type("ToolUseBlock", (), {"name": "Read", "id": i, "input": {}})()

    def res(i):
        return type(
            "ToolResultBlock",
            (),
            {"tool_use_id": i, "content": f"out-{i}", "is_error": False},
        )()

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def query(self, _prompt):
            return None

        async def receive_response(self):
            yield type("AssistantMessage", (), {"content": [use("a"), use("b")]})()
            yield type("UserMessage", (), {"content": [res("a"), res("b")]})()

    monkeypatch.setattr(agent_runner, "create_client", lambda *_a, **_kw: Client())
    transcript.start(tmp_path)
    try:
        runner = agent_runner.AgentRunner(
            tmp_path,
            tmp_path,
            "claude-sonnet-4-5",
            TaskLogger(tmp_path, emit_markers=False),
        )
        success, _ = await runner.run_agent("spec_gatherer.md")
        assert success
        rows = [
            json.loads(line)
            for line in transcript.written_files()[0].read_text().splitlines()
        ]
        assert [(r["kind"], r["tool_use_id"]) for r in rows] == [
            ("tool_start", "a"),
            ("tool_start", "b"),
            ("tool_end", "a"),
            ("tool_end", "b"),
        ]
    finally:
        transcript.reset()
