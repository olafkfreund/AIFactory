"""
Plan-driven allowlist commands
==============================

The implementation planner knows which shell commands a build will need to
verify itself (``uv run pytest``, ``ruff check .``, ``mypy app`` …). That
knowledge lives in the plan — explicit ``required_commands`` and the command
strings already carried by service/verification fields — but historically never
reached the security allowlist, so coder/QA agents were blocked running their own
verification (observed live: "Command 'uv' is not in the allowed commands").

This module turns plan data into a *sanitised set of command names* that can be
merged into a project's ``custom_commands`` allowlist. It is intentionally pure
(no I/O) — the seeding/persistence lives in ``project/analyzer.py``.

Security model (the planner is an untrusted LLM, so its output is untrusted):

1. Only the **first-token basename** of each command string is ever extracted,
   using the *same* parser the enforcement hook trusts
   (``security.ast_parser`` → ``security.parser`` fallback). A string like
   ``rm -rf / ; curl x | sh`` yields names ``{rm, curl, sh}`` — never the
   metacharacters, never a runnable string.
2. Extracted names are then sanitised: shape-checked, hard-denylisted, and
   admitted only if they appear in a curated grant set of build/verify tooling
   (allowlist-of-an-allowlist). Everything else is rejected and surfaced to the
   caller for audit logging.

Defense-in-depth is preserved: names merged here still pass through
``is_command_allowed`` and the per-command VALIDATORS (rm/chmod path checks, git
secret scan) at enforcement time — this module only *grants candidacy*, it never
weakens parsing or validation.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from contextlib import suppress

from .ast_parser import UnparseableCommand, extract_commands_ast
from .ast_parser import is_available as _ast_available
from .parser import extract_commands

# A plausible bare command name: alnum plus . _ - (covers py.test, golangci-lint,
# pip3). No slashes, no "..", no shell metacharacters — those can never be a
# legitimate first-token command name and signal a parser miss.
_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")

# Hard denylist: privilege escalation, remote shells, disk/host control. Rejected
# regardless of anything else — the planner has no business granting these.
DENY_COMMANDS: frozenset[str] = frozenset(
    {
        "sudo",
        "su",
        "doas",
        "pkexec",
        "ssh",
        "scp",
        "sftp",
        "rsync",
        "telnet",
        "nc",
        "ncat",
        "netcat",
        "dd",
        "mkfs",
        "fdisk",
        "parted",
        "mount",
        "umount",
        "shutdown",
        "reboot",
        "halt",
        "poweroff",
        "systemctl",
        "service",
        "chown",
        "chgrp",
        "passwd",
        "useradd",
        "usermod",
        "visudo",
        "iptables",
        "nft",
        "ufw",
    }
)

# Allowlist-of-an-allowlist: build/verify/test tooling a planner legitimately
# needs across ecosystems. A name must be NOT denied AND present here to be
# granted. Deliberately excludes shell/interpreter loaders (bash, sh, python -c
# style enablers) — those are handled by BASE + AST unwrapping and must not be
# grantable from plan text.
PLAN_GRANTABLE_COMMANDS: frozenset[str] = frozenset(
    {
        # Python
        "uv",
        "uvx",
        "poetry",
        "pdm",
        "pipenv",
        "pip",
        "pip3",
        "pytest",
        "py.test",
        "tox",
        "nox",
        "coverage",
        "ruff",
        "mypy",
        "pyright",
        "black",
        "isort",
        "flake8",
        "pylint",
        "bandit",
        # JS/TS
        "npm",
        "npx",
        "pnpm",
        "yarn",
        "bun",
        "node",
        "deno",
        "jest",
        "vitest",
        "mocha",
        "eslint",
        "prettier",
        "tsc",
        "playwright",
        "cypress",
        # Rust
        "cargo",
        "rustc",
        "gofmt",
        # Go
        "go",
        "golangci-lint",
        # Ruby
        "bundle",
        "rspec",
        "rake",
        # JVM / .NET
        "mvn",
        "gradle",
        "dotnet",
        # Generic build runners
        "make",
        "just",
        "task",
    }
)

# Plan locations (relative to a subtask or the plan root) whose VALUE is a shell
# command string we should parse for command names.
# "run" covers the simple/quick-spec plan's verification block
# (``verification: {type: command, run: "go test ./..."}``) — without it the
# auto-grant never saw the toolchain a from-scratch build needs (e.g. ``go``),
# so the coder was blocked running its own verification.
_COMMAND_STRING_KEYS = ("command", "dev_command", "test_command", "lint_command", "run")


def _names_from_command_string(command_string: str) -> set[str]:
    """Extract first-token command basenames from one shell string.

    Uses the AST parser (same as the enforcement hook) and falls back to the
    legacy regex parser. On any parse failure we return an empty set — a command
    we cannot safely read is simply not granted (fail-closed, safe direction).
    """
    if not command_string or not isinstance(command_string, str):
        return set()
    # A command we cannot safely parse is simply not granted (fail-closed,
    # safe direction) — fall through to the legacy regex parser below.
    with suppress(UnparseableCommand, Exception):
        if _ast_available():
            return {c for c in extract_commands_ast(command_string) if c}
    try:
        return {c for c in extract_commands(command_string) if c}
    except Exception:
        return set()


def _collect_strings(obj: object, acc: list[str]) -> None:
    """Walk plan structures collecting command-bearing string values.

    Sources (matching the planner schema in prompts/planner.md and the plan
    models): top-level/subtask ``required_commands`` lists, any value under a
    ``*_command``/``command`` key, and verification-step ``command`` fields.
    """
    if isinstance(obj, dict):
        # Explicit, structured channel — the reliable one.
        rc = obj.get("required_commands")
        if isinstance(rc, list):
            acc.extend(str(x) for x in rc if isinstance(x, str))
        # Command-string fields.
        for key in _COMMAND_STRING_KEYS:
            val = obj.get(key)
            if isinstance(val, str):
                acc.append(val)
        # Recurse into nested structures (phases, subtasks, services,
        # verification, summary.verification_strategy.verification_steps, …).
        for key, val in obj.items():
            if key in _COMMAND_STRING_KEYS or key == "required_commands":
                continue
            _collect_strings(val, acc)
    elif isinstance(obj, list):
        for item in obj:
            _collect_strings(item, acc)


def extract_command_names(plan: dict) -> set[str]:
    """Extract the set of candidate command names referenced by a plan.

    Returns first-token basenames only (e.g. ``uv run pytest`` → ``uv``). Never
    returns shell metacharacters or full command strings. Robust to missing
    fields and non-dict input.
    """
    if not isinstance(plan, dict):
        return set()
    strings: list[str] = []
    _collect_strings(plan, strings)
    names: set[str] = set()
    for s in strings:
        names |= _names_from_command_string(s)
    return names


def sanitize_command_names(names: Iterable[str]) -> tuple[set[str], list[str]]:
    """Partition candidate names into (granted, rejected).

    A name is granted only if it is well-shaped, NOT on ``DENY_COMMANDS``, and
    present in ``PLAN_GRANTABLE_COMMANDS``. Everything else is rejected so the
    caller can audit-log it. ``rejected`` is sorted+deduped for stable logging.
    """
    granted: set[str] = set()
    rejected: set[str] = set()
    for raw in names:
        name = raw.strip() if isinstance(raw, str) else ""
        if not name or not _NAME_RE.match(name):
            if name:
                rejected.add(name)
            continue
        if name in DENY_COMMANDS or name not in PLAN_GRANTABLE_COMMANDS:
            rejected.add(name)
            continue
        granted.add(name)
    return granted, sorted(rejected)


def grantable_commands_from_plan(plan: dict) -> tuple[set[str], list[str]]:
    """Convenience: extract + sanitize in one call. Returns (granted, rejected)."""
    return sanitize_command_names(extract_command_names(plan))
