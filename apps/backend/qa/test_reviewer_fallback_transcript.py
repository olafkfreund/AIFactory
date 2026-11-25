"""The QA tool-fallback provider's tool calls reach the run transcript (#2054)."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from qa import reviewer
from task_logger import TaskLogger, transcript


class ToolUseBlock(SimpleNamespace):
    pass


class ToolResultBlock(SimpleNamespace):
    pass


class AssistantMessage(SimpleNamespace):
    pass


class UserMessage(SimpleNamespace):
    pass


class FakeProvider:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def query(self, _prompt):
        pass

    async def receive_response(self):
        yield AssistantMessage(
            content=[
                ToolUseBlock(
                    id="t1", name="Write", input={"k": "sk-ant-abcdefghij1234"}
                )
            ]
        )
        yield UserMessage(
            content=[ToolResultBlock(tool_use_id="t1", content="ok", is_error=False)]
        )


@pytest.fixture(autouse=True)
def _reset():
    transcript.reset()
    yield
    transcript.reset()


def test_fallback_tool_calls_are_recorded_and_redacted(tmp_path, monkeypatch):
    monkeypatch.setattr(reviewer, "get_provider", lambda *_a, **_k: FakeProvider())
    monkeypatch.setattr(
        reviewer, "get_qa_signoff_status", lambda _d: {"status": "approved"}
    )
    transcript.start(tmp_path)
    logger = TaskLogger(tmp_path)
    out = asyncio.run(
        reviewer._run_tool_fallback("analysis", tmp_path, tmp_path, 1, logger)
    )
    assert out == ("approved", "analysis")
    path = transcript.written_files()[0]
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert [(r["kind"], r["tool"], r["success"]) for r in rows] == [
        ("tool_start", "Write", None),
        ("tool_end", "Write", True),
    ]
    assert "sk-ant-abcdefghij1234" not in path.read_text()
