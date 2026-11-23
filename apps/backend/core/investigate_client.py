"""Isolated SDK client for investigation pods."""

from collections.abc import Awaitable, Callable
from typing import Any

from agents.investigate_guard import can_use_tool, pre_tool_use
from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    create_sdk_mcp_server,
    tool,
)
from claude_agent_sdk.types import HookMatcher
from core.auth import get_agent_env_blanks


def create_investigate_client(
    turns: int, run_tests: Callable[[], Awaitable[dict[str, Any]]]
) -> ClaudeSDKClient:
    @tool("run_tests", "Run the configured repository tests in an isolated pod.", {})
    async def run_tests_tool(args: dict[str, Any]) -> dict[str, Any]:
        del args
        result = await run_tests()
        return {"content": [{"type": "text", "text": str(result)}]}

    options = ClaudeAgentOptions(
        cwd="/src",
        env={
            **get_agent_env_blanks(),
            "AIFACTORY_RUN_TOKEN": "",
            # Deferred tools need ToolSearch, which the tool boundary denies (#2196).
            "ENABLE_TOOL_SEARCH": "false",
        },
        setting_sources=[],  # never load a repository's .claude/ settings, hooks or MCP
        permission_mode="default",
        allowed_tools=["mcp__investigate__run_tests"],
        disallowed_tools=[
            "Write",
            "Edit",
            "MultiEdit",
            "NotebookEdit",
            "Bash",
            "WebFetch",
            "WebSearch",
            "Task",
            "TodoWrite",
        ],
        mcp_servers={
            "investigate": create_sdk_mcp_server("investigate", tools=[run_tests_tool])
        },
        max_turns=turns,
        include_partial_messages=True,
        can_use_tool=can_use_tool,
        hooks={"PreToolUse": [HookMatcher(hooks=[pre_tool_use])]},
    )
    return ClaudeSDKClient(options=options)
