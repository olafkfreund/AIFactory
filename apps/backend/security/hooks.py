"""
Security Hooks
==============

Pre-tool-use hooks that validate agent tool calls:
- ``bash_security_hook``: command allowlist + fail-closed parsing (Bash tool).
- ``web_fetch_security_hook``: SSRF guard on WebFetch URLs (#370).

Main enforcement point for the security system.

These checks are guardrails, not containment: a script, recipe, interpreter
or test runner the allowlist permits can run anything inside it. The Job pod
is the boundary (#1932). Make targets and package scripts are pinned to the
build's base commit (#1948, ``security/script_validators.py``).
"""

import logging
import os
from pathlib import Path
from typing import Any

from project_analyzer import BASE_COMMANDS, SecurityProfile, is_command_allowed

logger = logging.getLogger(__name__)

from .ast_parser import UnparseableCommand, extract_command_segments_ast, is_available
from .egress import check_egress
from .exec_context import reset_worktree_root, set_worktree_root
from .filesystem_validators import protected_write_refusal  # noqa: E402
from .profile import get_security_profile
from .url_guard import assert_url_not_ssrf
from .validator import VALIDATORS

_GIT_DIR_WRITE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})


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


def _extract_commands_fail_closed(
    command: str,
) -> tuple[list[tuple[str, str, bool]] | None, str]:
    """Extract ``(name, segment, dynamic_args)`` with the fail-closed AST parser.

    Returns ``(commands, "")`` on success, or ``(None, reason)`` when the
    command must be blocked because it could not be safely parsed, or when
    ``bashlex`` is unavailable (degraded install).
    """
    if is_available():
        try:
            return extract_command_segments_ast(command), ""
        except UnparseableCommand as exc:
            return None, (
                "Command blocked: could not be safely parsed for security "
                f"validation ({exc}). Rewrite it without dynamic shell "
                'payloads (e.g. avoid `bash -c "$VAR"`).'
            )
    # Degraded install: bashlex is a hard requirement, so refuse everything
    # rather than trust the regex parser (it cannot see through wrappers,
    # substitutions or quoting).
    return None, "Command blocked: bash commands cannot be validated without bashlex."


def _validator_refusal(cmds: list[tuple[str, str, bool]], root: str) -> str | None:
    """Run the per-command validators on each unwrapped segment.

    Returns the refusal reason, or None when every validator passes. Arguments
    xargs or ``find -exec`` append at run time cannot be checked, so a
    validated command reached that way is refused.
    """
    for name, seg, dynamic_args in cmds:
        if name not in VALIDATORS:
            continue
        token = set_worktree_root(root)
        try:
            allowed, reason = VALIDATORS[name](seg)
        finally:
            reset_worktree_root(token)
        if not allowed:
            return reason
        if dynamic_args:
            return (
                f"`{name}` run by xargs or find -exec takes arguments the "
                f"{name} validator cannot check. Pass explicit arguments "
                "instead (or use `find -delete`)."
            )
    return None


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

    # shortcut: text match only, an obfuscated path gets past it; enforcement no longer
    # reads the file (#1944), so this is defence in depth.
    if any(
        n in command.casefold()
        for n in (".aifactory-security.json", ".aifactory-allowlist")
    ):
        return {
            "decision": "block",
            "reason": "Refused: the security profile is managed by AIFactory.",
        }

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

    names = [n for n, _, _ in commands]
    for cmd in names:
        is_allowed, reason = is_command_allowed(cmd, profile)
        if not is_allowed:
            return {
                "decision": "block",
                "reason": reason,
            }

    # Expose the worktree root (cwd) so path validators (rm/chmod) can reject
    # escapes (#364).
    if reason := _validator_refusal(commands, cwd):
        return {"decision": "block", "reason": reason}

    # Egress policy (#363 AC3): defense-in-depth over the allowlist. When opted
    # in (AIFACTORY_EGRESS_POLICY=deny|allowlist) an allowlisted network tool
    # (curl/wget/ssh/…) is still blocked unless the target host is permitted —
    # so a prompt-injected command can't exfiltrate even though the binary is
    # allowed. Default off → no behavior change.
    egress_ok, egress_reason = check_egress(command, names)
    if not egress_ok:
        return {"decision": "block", "reason": egress_reason}

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

    for cmd in (n for n, _, _ in commands):
        is_allowed_result, reason = is_command_allowed(cmd, profile)
        if not is_allowed_result:
            return False, reason

    if reason := _validator_refusal(commands, str(project_dir)):
        return False, reason

    return True, ""


async def git_dir_write_hook(
    input_data: dict[str, Any],
    _tool_use_id: str | None = None,
    _context: Any | None = None,
    *,
    project_dir: str | None = None,
) -> dict[str, Any]:
    """Refuse file-tool writes to protected paths.

    Covers .git (#1719), the profile file (#1944) and the source .aifactory (#1985).
    ``project_dir`` is bound by create_client; the input's cwd moves with ``cd``.
    """
    if input_data.get("tool_name") not in _GIT_DIR_WRITE_TOOLS:
        return {}
    tool_input = input_data.get("tool_input")
    if not isinstance(tool_input, dict):
        return {"decision": "block", "reason": "Malformed tool input."}
    # Check every path key present: NotebookEdit writes notebook_path even if
    # a harmless file_path rides along.
    paths = [tool_input[k] for k in ("file_path", "notebook_path") if k in tool_input]
    if not paths or not all(isinstance(p, str) and p for p in paths):
        return {"decision": "block", "reason": "Missing or invalid file path."}
    for path in paths:
        refusal = protected_write_refusal(path, input_data.get("cwd"), project_dir)
        if refusal:
            logger.warning("Blocked agent write into protected path")
            return {"decision": "block", "reason": refusal}
    return {}
