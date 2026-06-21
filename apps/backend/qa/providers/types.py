"""Backward compatibility shim — re-exports from providers.types."""

from providers.types import (  # noqa: F401
    AssistantMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

__all__ = [
    "AssistantMessage",
    "TextBlock",
    "ToolResultBlock",
    "ToolUseBlock",
    "UserMessage",
]
