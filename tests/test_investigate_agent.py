import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "backend"))

# tests/conftest.py replaces claude_agent_sdk with a mock. These tests need the real
# types (isinstance checks, ClaudeAgentOptions), so import them with the mock lifted,
# then put the mock back for the rest of the suite.
_SDK_MOCKS = {k: v for k, v in sys.modules.items() if k.startswith("claude_agent_sdk")}
for _name in _SDK_MOCKS:
    del sys.modules[_name]
from agents import investigate, investigate_guard  # noqa: E402
from core.investigate_client import create_investigate_client  # noqa: E402

sys.modules.update(_SDK_MOCKS)

AssistantMessage = investigate.AssistantMessage
StreamEvent = investigate.StreamEvent
TextBlock = investigate.TextBlock
ToolUseBlock = investigate.ToolUseBlock


def test_hook_blocks_tools_and_escaped_paths(tmp_path, monkeypatch):
    root = tmp_path / "src"
    root.mkdir()
    (root / "README.md").write_text("ok")
    (root / "escape").symlink_to(tmp_path)
    monkeypatch.setattr(investigate_guard, "_ROOT", str(root))
    for name, args in [
        ("Write", {"file_path": str(root / "x")}),
        ("Bash", {}),
        ("mcp__other__tool", {}),
        ("Read", {"file_path": "/proc/self/environ"}),
        ("Read", {"file_path": "/src/../etc/passwd"}),
        ("Read", {"file_path": str(root / "escape" / "x")}),
        ("Glob", {"pattern": "/**"}),
        ("Glob", {"pattern": "../*"}),
        ("Grep", {"path": "/tmp"}),
    ]:
        assert not investigate_guard.permitted(name, args)
        assert (
            asyncio.run(
                investigate_guard.pre_tool_use({"tool_name": name, "tool_input": args})
            )["hookSpecificOutput"]["permissionDecision"]
            == "deny"
        )
    assert investigate_guard.permitted("Read", {"file_path": str(root / "README.md")})
    assert investigate_guard.permitted("Grep", {"path": str(root / "a")})
    assert investigate_guard.permitted(
        "Grep", {"pattern": "/api/users", "path": str(root)}
    )
    assert not investigate_guard.permitted("Grep", {"pattern": "x", "glob": "/etc/*"})
    assert not investigate_guard.permitted("Grep", {"pattern": "x", "glob": "../*"})


def test_options():
    # setting_sources=[] keeps a repository's own .claude/ configuration out.
    client = create_investigate_client(7, lambda: None)
    options = client.options
    assert options.cwd == "/src"
    assert options.setting_sources == []
    assert options.permission_mode == "default"
    assert options.allowed_tools == ["mcp__investigate__run_tests"]
    assert options.max_turns == 7
    assert options.include_partial_messages is True
    assert set(options.mcp_servers) == {"investigate"}
    assert options.can_use_tool is investigate_guard.can_use_tool
    assert len(options.hooks["PreToolUse"]) == 1


def test_token_accounting_distinct_ids_and_cumulative_output():
    responses = {}
    start = {
        "type": "message_start",
        "message": {
            "id": "a",
            "usage": {"input_tokens": 2, "cache_creation_input_tokens": 3},
        },
    }
    assert investigate._count_tokens(start, responses) == 5
    assert investigate._count_tokens(start, responses) == 5
    assert (
        investigate._count_tokens(
            {"type": "message_delta", "usage": {"output_tokens": 4}}, responses
        )
        == 9
    )
    assert (
        investigate._count_tokens(
            {"type": "message_delta", "usage": {"output_tokens": 6}}, responses
        )
        == 11
    )
    assert (
        investigate._count_tokens(
            {
                "type": "message_start",
                "message": {"id": "b", "usage": {"input_tokens": 1}},
            },
            responses,
        )
        == 12
    )


class FakeAPI:
    def __init__(self, tokens=10):
        self.config = {
            "request": "find a bug",
            "sha": "a" * 40,
            "limits": {"tokens": tokens, "turns": 3},
        }
        self.calls = []

    async def request(self, method, path, data=None):
        self.calls.append((method, path, data))
        return self.config if path == "config" else {}

    def lease(self):
        return {"ok": True}


class FakeClient:
    def __init__(self, events):
        self.events = events
        self.interrupted = False
        self.prompt = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def query(self, prompt):
        self.prompt = prompt

    async def receive_response(self):
        for event in self.events:
            yield event

    async def interrupt(self):
        self.interrupted = True


def test_budget_interrupts_before_end_and_log_has_no_file_contents():
    api = FakeAPI(tokens=5)
    events = [
        StreamEvent(
            "1",
            "s",
            {
                "type": "message_start",
                "message": {"id": "a", "usage": {"input_tokens": 5}},
            },
        ),
        AssistantMessage(
            [
                ToolUseBlock("t", "Read", {"file_path": "/src/README.md"}),
                TextBlock("secret file content"),
            ],
            "model",
        ),
        StreamEvent("2", "s", {"type": "message_delta", "usage": {"output_tokens": 1}}),
    ]
    client = FakeClient(events)
    reason = asyncio.run(investigate.run(api, lambda turns, run_tests: client))
    assert reason == "token budget" and client.interrupted
    assert "find a bug" in client.prompt
    assert not any(path == "report" for _, path, _ in api.calls)
    assert "secret file content" not in str(api.calls)


def test_watchdog_stops_after_three_failures():
    class FailingAPI:
        def lease(self):
            raise ValueError("unavailable")

    class NoWait:
        def wait(self, seconds):
            return False

    import threading

    failed = threading.Event()
    woke = []
    investigate._watchdog(FailingAPI(), NoWait(), failed, lambda: woke.append(True))
    assert failed.is_set() and woke == [True]


def test_watchdog_stops_on_revoked_lease():
    class RevokedAPI:
        def lease(self):
            return {"ok": False}

    class NoWait:
        def wait(self, seconds):
            return False

    import threading

    failed = threading.Event()
    investigate._watchdog(RevokedAPI(), NoWait(), failed, lambda: None)
    assert failed.is_set()
