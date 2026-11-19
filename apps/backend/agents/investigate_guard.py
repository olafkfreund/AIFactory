"""Tool boundary for the read-only investigation agent."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from claude_agent_sdk.types import PermissionResultAllow, PermissionResultDeny

_ROOT = "/src"
_TOOLS = {"Read", "Glob", "Grep", "mcp__investigate__run_tests"}


def permitted(name: str, arguments: dict[str, Any]) -> bool:
    if name not in _TOOLS:
        return False
    if name == "mcp__investigate__run_tests":
        return True
    # Glob's ``pattern`` and Grep's ``glob`` are paths; Grep's ``pattern`` is a regex.
    for key in {"Glob": ("pattern",), "Grep": ("glob",)}.get(name, ()):
        value = arguments.get(key)
        if value is not None and (
            not isinstance(value, str)
            or Path(value).is_absolute()
            or ".." in value.split("/")
        ):
            return False
    key = "file_path" if name == "Read" else "path"
    path = arguments.get(key, ".")
    if not isinstance(path, str) or not path:
        return False
    resolved = os.path.realpath(Path(_ROOT) / path)
    return resolved == _ROOT or resolved.startswith(_ROOT + os.sep)


async def can_use_tool(name: str, arguments: dict[str, Any], context: Any) -> Any:
    del context
    if permitted(name, arguments):
        return PermissionResultAllow()
    return PermissionResultDeny(message="Investigation tool boundary")


async def pre_tool_use(
    input_data: dict[str, Any], tool_use_id: str | None = None, context: Any = None
) -> dict[str, Any]:
    del tool_use_id, context
    allowed = permitted(
        input_data.get("tool_name", ""), input_data.get("tool_input") or {}
    )
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow" if allowed else "deny",
            "permissionDecisionReason": "Investigation tool boundary"
            if not allowed
            else "",
        }
    }
