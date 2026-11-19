"""Tool input shown in the task log is described and redacted (#2151)."""

import asyncio
import json
from types import SimpleNamespace

from qa import reviewer
from task_logger import LogPhase, TaskLogger, describe_tool_input, scrub

TOKEN = "ghp_" + "a1B2c3D4e5" * 4  # GitHub PAT shape, synthetic
PEM = "-----BEGIN RSA PRIVATE KEY-----\nMIIEabc\n-----END RSA PRIVATE KEY-----"


def test_preferred_keys_and_empty():
    assert describe_tool_input({"command": "ls -la"}) == "ls -la"
    assert describe_tool_input({"pattern": "foo"}) == "pattern: foo"
    assert describe_tool_input({"file_path": "/" + "a" * 200}).startswith("...")
    assert describe_tool_input({}) is None
    assert describe_tool_input(None) is None
    assert scrub(None) is None


def test_other_tools_fall_back_to_json_with_masked_keys():
    out = describe_tool_input(
        {"command": None, "args": {"Password": "plain-secret-1", "n": [1]}}
    )
    assert out == '{"command":null,"args":{"Password":"***REDACTED***","n":[1]}}'
    assert "plain-secret-1" not in out
    assert describe_tool_input({"query": ["x"]}) == '{"query":["x"]}'


def test_secrets_removed_before_shortening():
    cmd = "x" * 90 + " " + TOKEN
    out = describe_tool_input({"command": cmd})
    assert TOKEN not in out and TOKEN[:10] not in out
    assert "MIIEabc" not in describe_tool_input({"content": PEM})


def test_tool_start_redacts_content_input_and_marker(tmp_path, capsys):
    logger = TaskLogger(tmp_path)
    logger.tool_start("Bash", "y" * 80 + " " + TOKEN, LogPhase.CODING)
    logger.tool_start("ToolSearch")
    entries = [
        e for p in logger.storage.get_data()["phases"].values() for e in p["entries"]
    ]
    assert entries
    for e in entries:
        assert TOKEN[:8] not in json.dumps(e)
    assert TOKEN[:8] not in capsys.readouterr().out


class ToolUseBlock(SimpleNamespace):
    pass


class AssistantMessage(SimpleNamespace):
    pass


class FakeClient:
    async def query(self, _prompt):
        pass

    async def receive_response(self):
        yield AssistantMessage(
            content=[
                ToolUseBlock(id="t1", name="Bash", input={"command": "cargo test"})
            ]
        )


def test_reviewer_session_logs_bash_command(tmp_path, monkeypatch):
    async def no_context(*_a, **_k):
        return None

    monkeypatch.setattr(reviewer, "get_graphiti_context", no_context)
    monkeypatch.setattr(reviewer, "get_qa_reviewer_prompt", lambda *_a: "p")
    monkeypatch.setattr(reviewer, "get_task_logger", lambda d: TaskLogger(d))
    asyncio.run(reviewer.run_qa_agent_session(FakeClient(), tmp_path, tmp_path, 1, 1))
    logs = json.loads((tmp_path / "task_logs.json").read_text())
    starts = [
        e
        for p in logs["phases"].values()
        for e in p["entries"]
        if e["type"] == "tool_start"
    ]
    assert [e.get("tool_input") for e in starts] == ["cargo test"]
