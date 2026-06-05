"""
Process Management Validators
==============================

Validators for process management commands (pkill, kill, killall).
"""

import shlex

from .validation_models import ValidationResult

# Allowed development process names
ALLOWED_PROCESS_NAMES = {
    # Node.js ecosystem
    "node",
    "npm",
    "npx",
    "yarn",
    "pnpm",
    "bun",
    "deno",
    "vite",
    "next",
    "nuxt",
    "webpack",
    "esbuild",
    "rollup",
    "tsx",
    "ts-node",
    # Python ecosystem
    "python",
    "python3",
    "flask",
    "uvicorn",
    "gunicorn",
    "django",
    "celery",
    "streamlit",
    "gradio",
    "pytest",
    "mypy",
    "ruff",
    # Other languages
    "cargo",
    "rustc",
    "go",
    "ruby",
    "rails",
    "php",
    # Databases (local dev)
    "postgres",
    "mysql",
    "mongod",
    "redis-server",
}


def validate_pkill_command(command_string: str) -> ValidationResult:
    """
    Validate pkill commands - only allow killing dev-related processes.

    Args:
        command_string: The full pkill command string

    Returns:
        Tuple of (is_valid, error_message)
    """
    try:
        tokens = shlex.split(command_string)
    except ValueError:
        return False, "Could not parse pkill command"

    if not tokens:
        return False, "Empty pkill command"

    # Separate flags from arguments
    args = []
    for token in tokens[1:]:
        if not token.startswith("-"):
            args.append(token)

    if not args:
        return False, "pkill requires a process name"

    # The target is typically the last non-flag argument
    target = args[-1]

    # For -f flag (full command line match), extract the first word
    if " " in target:
        target = target.split()[0]

    if target in ALLOWED_PROCESS_NAMES:
        return True, ""
    return (
        False,
        f"pkill only allowed for dev processes: {sorted(ALLOWED_PROCESS_NAMES)[:10]}...",
    )


def validate_kill_command(command_string: str) -> ValidationResult:
    """
    Validate kill commands — every target must be a positive PID (#364).

    Rejects ``kill -1`` / ``kill 0`` (all processes) and ``kill -- -<pgid>``
    (process-group kills): a negative or zero target signals "more than one
    process" and is never a single agent-spawned PID. Signal flags (``-9``,
    ``-s TERM``) are allowed; only the PID operands are constrained.

    Note: without a registry of agent-spawned PIDs we can't prove ownership of a
    given positive PID — that's a separate follow-up. This closes the broad
    "kill everything / a whole process group" vectors.
    """
    try:
        tokens = shlex.split(command_string)
    except ValueError:
        return False, "Could not parse kill command"

    # Parse kill's grammar: an optional leading signal, then PID operands.
    # Only the first argument may be a signal — so a later ``-1`` is a negative
    # PID (a process group / "all processes"), NOT a signal flag.
    i = 1
    if i < len(tokens) and tokens[i] != "--" and tokens[i].startswith("-"):
        if tokens[i] in ("-s", "--signal", "-n"):
            i += 2  # signal name/number is the next token
        else:
            i += 1  # a signal spec like -9 / -TERM / -SIGKILL
    if i < len(tokens) and tokens[i] == "--":
        i += 1
    pids = tokens[i:]

    if not pids:
        return False, "kill requires a target PID"
    for pid in pids:
        if not pid.isdigit() or int(pid) <= 0:
            return (
                False,
                f"kill target {pid!r} must be a single positive PID — process "
                "groups, -1 and 0 (which signal all processes) are not allowed",
            )

    return True, ""


def validate_killall_command(command_string: str) -> ValidationResult:
    """
    Validate killall commands - same rules as pkill.

    Args:
        command_string: The full killall command string

    Returns:
        Tuple of (is_valid, error_message)
    """
    return validate_pkill_command(command_string)
