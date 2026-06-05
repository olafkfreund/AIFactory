"""
Security Hooks
==============

Pre-tool-use hooks that validate agent tool calls:
- ``bash_security_hook``: command allowlist + fail-closed parsing (Bash tool).
- ``web_fetch_security_hook``: SSRF guard on WebFetch URLs (#370).

Main enforcement point for the security system.
"""

import logging
import os
from pathlib import Path
from typing import Any

from project_analyzer import BASE_COMMANDS, SecurityProfile, is_command_allowed

logger = logging.getLogger(__name__)

from .ast_parser import UnparseableCommand, extract_commands_ast, is_available
from .parser import extract_commands, get_command_for_validation, split_command_segments
from .profile import get_security_profile
from .url_guard import assert_url_not_ssrf
from .validator import VALIDATORS


async def web_fetch_security_hook(
    input_data: dict[str, Any],
    tool_use_id: str | None = None,
    context: Any | None = None,
) -> dict[str, Any]:
    """Pre-tool-use hook that blocks SSRF via the agent's ``WebFetch`` tool.

    The agent runs under ``bypassPermissions`` with ``WebFetch`` granted; a
    prompt-injected/LLM-chosen URL would otherwise reach cloud metadata,
    loopback, or internal services unchecked (#370). ``WebSearch`` carries a
    ``query`` (no URL) so it passes through; any URL it surfaces is fetched via
    ``WebFetch``, which this hook gates.

    Returns ``{}`` to allow, or ``{"decision": "block", "reason": ...}``.
    """
    if input_data.get("tool_name") not in ("WebFetch", "WebSearch"):
        return {}

    tool_input = input_data.get("tool_input")
    if not isinstance(tool_input, dict):
        return {}

    url = tool_input.get("url")
    if not url or not isinstance(url, str):
        # WebSearch (query only) or a malformed call with no URL to validate.
        return {}

    try:
        assert_url_not_ssrf(url)
    except ValueError as exc:
        logger.warning("Blocked WebFetch to unsafe URL %r: %s", url, exc)
        return {
            "decision": "block",
            "reason": (
                f"WebFetch blocked: {exc}. Only public http(s) URLs are "
                "permitted; private/loopback/link-local/metadata hosts and "
                "non-http schemes are refused."
            ),
        }
    return {}


def _extract_commands_fail_closed(command: str) -> tuple[list[str] | None, str]:
    """Extract command names with the fail-closed AST parser.

    Returns ``(commands, "")`` on success, or ``(None, reason)`` when the
    command must be blocked because it could not be safely parsed. Falls back
    to the legacy regex parser only when ``bashlex`` is unavailable (degraded
    install) — never to widen what the AST parser would have blocked.
    """
    if is_available():
        try:
            return extract_commands_ast(command), ""
        except UnparseableCommand as exc:
            return None, (
                "Command blocked: could not be safely parsed for security "
                f"validation ({exc}). Rewrite it without dynamic shell "
                'payloads (e.g. avoid `bash -c "$VAR"`).'
            )
    # Degraded fallback: bashlex not installed.
    return extract_commands(command), ""


async def bash_security_hook(
    input_data: dict[str, Any],
    tool_use_id: str | None = None,
    context: Any | None = None,
) -> dict[str, Any]:
    """
    Pre-tool-use hook that validates bash commands using dynamic allowlist.

    This is the main security enforcement point. It:
    1. Validates tool_input structure (must be dict with 'command' key)
    2. Extracts command names from the command string
    3. Checks each command against the project's security profile
    4. Runs additional validation for sensitive commands
    5. Blocks disallowed commands with clear error messages

    Args:
        input_data: Dict containing tool_name and tool_input
        tool_use_id: Optional tool use ID
        context: Optional context

    Returns:
        Empty dict to allow, or {"decision": "block", "reason": "..."} to block
    """
    if input_data.get("tool_name") != "Bash":
        return {}

    # Validate tool_input structure before accessing
    tool_input = input_data.get("tool_input")

    # Check if tool_input is None (malformed tool call)
    if tool_input is None:
        return {
            "decision": "block",
            "reason": "Bash tool_input is None - malformed tool call from SDK",
        }

    # Check if tool_input is a dict
    if not isinstance(tool_input, dict):
        return {
            "decision": "block",
            "reason": f"Bash tool_input must be dict, got {type(tool_input).__name__}",
        }

    # Now safe to access command
    command = tool_input.get("command", "")
    if not command:
        return {}

    # Get the working directory from input_data (SDK passes it there, not in context)
    cwd = input_data.get("cwd") or os.getcwd()

    # Get or create security profile
    # Note: In actual use, spec_dir would be passed through context
    try:
        profile = get_security_profile(Path(cwd))
    except Exception as e:
        # If profile creation fails, fall back to base commands only
        print(f"Warning: Could not load security profile: {e}")
        logger.warning(f"Could not load security profile: {e}", exc_info=True)
        profile = SecurityProfile()
        profile.base_commands = BASE_COMMANDS.copy()

    # Extract all commands from the command string (fail closed on anything
    # we cannot safely parse — see ast_parser / #321).
    commands, block_reason = _extract_commands_fail_closed(command)
    if commands is None:
        return {"decision": "block", "reason": block_reason}

    if not commands:
        # Could not parse - fail safe by blocking
        return {
            "decision": "block",
            "reason": f"Could not parse command for security validation: {command}",
        }

    # Split into segments for per-command validation
    segments = split_command_segments(command)

    # Get all allowed commands
    allowed = profile.get_all_allowed_commands()

    # Check each command against the allowlist
    for cmd in commands:
        # Check if command is allowed
        is_allowed, reason = is_command_allowed(cmd, profile)

        if not is_allowed:
            return {
                "decision": "block",
                "reason": reason,
            }

        # Additional validation for sensitive commands
        if cmd in VALIDATORS:
            cmd_segment = get_command_for_validation(cmd, segments)
            if not cmd_segment:
                cmd_segment = command

            validator = VALIDATORS[cmd]
            allowed, reason = validator(cmd_segment)
            if not allowed:
                return {"decision": "block", "reason": reason}

    return {}


def validate_command(
    command: str,
    project_dir: Path | None = None,
) -> tuple[bool, str]:
    """
    Validate a command string (for testing/debugging).

    Args:
        command: Full command string to validate
        project_dir: Optional project directory (uses cwd if not provided)

    Returns:
        (is_allowed, reason) tuple
    """
    if project_dir is None:
        project_dir = Path.cwd()

    profile = get_security_profile(project_dir)
    commands, block_reason = _extract_commands_fail_closed(command)
    if commands is None:
        return False, block_reason

    if not commands:
        return False, "Could not parse command"

    segments = split_command_segments(command)

    for cmd in commands:
        is_allowed_result, reason = is_command_allowed(cmd, profile)
        if not is_allowed_result:
            return False, reason

        if cmd in VALIDATORS:
            cmd_segment = get_command_for_validation(cmd, segments)
            if not cmd_segment:
                cmd_segment = command

            validator = VALIDATORS[cmd]
            allowed, reason = validator(cmd_segment)
            if not allowed:
                return False, reason

    return True, ""
