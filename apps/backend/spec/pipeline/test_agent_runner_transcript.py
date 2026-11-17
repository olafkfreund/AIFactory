"""Spec pipeline tool calls appear in the run transcript."""

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from task_logger import TaskLogger, transcript

from . import agent_runner


def _use(tool_id: str, **inp: str) -> Any:
    # The runner checks message class names supplied by the SDK.
    return type("ToolUseBlock", (), {"name": "Read", "id": tool_id, "input": inp})()


def _result(tool_id: str, content: str) -> Any:
    return type(
        "ToolResultBlock",
        (),
        {"tool_use_id": tool_id, "content": content, "is_error": False},
    )()


def _client(uses: list[Any], results: list[Any]) -> Any:
    class Client:
        async def __aenter__(self) -> "Client":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def query(self, _prompt: str) -> None:
            return None

        async def receive_response(self) -> AsyncIterator[Any]:
            yield type("AssistantMessage", (), {"content": uses})()
            yield type("UserMessage", (), {"content": results})()

    return Client()


async def _rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, client: Any
) -> list[dict[str, Any]]:
    monkeypatch.setattr(agent_runner, "create_client", lambda *_a, **_kw: client)
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
        text = transcript.written_files()[0].read_text()
        return [json.loads(line) for line in text.splitlines()]
    finally:
        transcript.reset()


@pytest.mark.asyncio
async def test_tool_events_are_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(
        [_use("tool-1", file_path="example.txt")], [_result("tool-1", "ok")]
    )
    rows = await _rows(tmp_path, monkeypatch, client)
    assert [(r["kind"], r["tool"], r["tool_use_id"]) for r in rows] == [
        ("tool_start", "Read", "tool-1"),
        ("tool_end", "Read", "tool-1"),
    ]
    assert rows[0]["payload"] == {"file_path": "example.txt"}
    assert rows[1]["payload"] == "ok"


@pytest.mark.asyncio
async def test_every_result_of_a_multi_tool_message_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(
        [_use("a"), _use("b")], [_result("a", "out-a"), _result("b", "out-b")]
    )
    rows = await _rows(tmp_path, monkeypatch, client)
    assert [(r["kind"], r["tool_use_id"]) for r in rows] == [
        ("tool_start", "a"),
        ("tool_start", "b"),
        ("tool_end", "a"),
        ("tool_end", "b"),
    ]
