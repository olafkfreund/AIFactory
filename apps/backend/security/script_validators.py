"""
Script Runner Validators
========================

Validators for ``make`` and ``npm``/``yarn``/``pnpm`` that enforce target and
script names against the security profile's base-commit script lists (#1948).
"""

from __future__ import annotations

import json
import shlex
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING

from .exec_context import get_worktree_root
from .validation_models import ValidationResult

if TYPE_CHECKING:
    from project.models import CustomScripts

_REFUSED_MAKE_LONG = frozenset(
    {"--file", "--makefile", "--directory", "--eval", "--include-dir"}
)
_REFUSED_MAKE_SHORT = frozenset("fCEI")
# Command-line variables that change what make reads or runs, whatever the recipe.
_REFUSED_MAKE_VARS = frozenset(
    {"SHELL", ".SHELLFLAGS", "MAKEFILES", "MAKEFLAGS", "GNUMAKEFLAGS"}
)
_MAKE_NUMBER_OPTS = frozenset({"--jobs", "--load-average", "--max-load"})

_REFUSED_PKG_OPTS = frozenset(
    {
        "--prefix",
        "-C",
        "--cwd",
        "--dir",
        "--filter",
        "-F",
        "-w",
        "--workspace",
        "-ws",
        "--workspaces",
        "-r",
        "--recursive",
        "--workspace-root",
        "--include-workspace-root",
        "--script-shell",
    }
)
# Options that take a separate value, so the value is not read as the subcommand.
_PKG_VALUE_OPTS = frozenset(
    {"--loglevel", "--registry", "--prefix", "-C", "--cwd", "--dir"}
    | {"--filter", "-F", "-w", "--workspace", "--script-shell"}
)
_REFUSED_PKG_SUBCOMMANDS = frozenset(
    {"workspace", "workspaces", "recursive", "multi", "m"}
)
_RUN_SUBCOMMANDS = frozenset({"run", "run-script", "rum", "urn"})
_TEST_SUBCOMMANDS = frozenset(
    {"test", "t", "tst", "it", "install-test", "cit", "install-ci-test"}
    | {"clean-install-test", "sit"}
)
_LIFECYCLE_SUBCOMMANDS = frozenset({"start", "stop", "restart"})


def _load_custom_scripts() -> CustomScripts | None:
    root = get_worktree_root()
    if root is None:
        return None
    import security.profile  # noqa: PLC0415

    return security.profile.get_security_profile(Path(root)).custom_scripts


def _is_refused_make_option(tok: str) -> bool:
    if tok.startswith("--"):
        # GNU make accepts any unambiguous abbreviation (`--fil=x.mk`).
        name = tok.split("=", 1)[0]
        return any(full.startswith(name) for full in _REFUSED_MAKE_LONG)
    return bool(_REFUSED_MAKE_SHORT & set(tok[1:]))


def _takes_number(prev: str) -> bool:
    """True when ``prev`` is ``-j``/``-l`` (or a cluster ending in one) or its long form."""
    if prev.startswith("--"):
        return prev in _MAKE_NUMBER_OPTS
    return prev.startswith("-") and prev[-1] in "jl"


def _make_token_refusal(prev: str, tok: str, allowed: set[str]) -> str:
    """Reason to refuse one make argument, or an empty string."""
    if tok == "--":
        return ""
    if tok.startswith("-") and tok != "-":
        refused = _is_refused_make_option(tok)
        return f"make option '{tok}' is not allowed" if refused else ""
    if "=" in tok:
        var = tok.split("=", 1)[0].rstrip(":+?!")
        refused = var in _REFUSED_MAKE_VARS
        return f"make variable '{var}' is not allowed" if refused else ""
    if (tok.isdigit() and _takes_number(prev)) or tok in allowed:
        return ""
    return f"make target '{tok}' is not in the base commit's Makefile"


def validate_make_command(seg: str) -> ValidationResult:
    """Validate that ``make`` only runs targets from the base commit's Makefile."""
    try:
        tokens = shlex.split(seg)
    except ValueError:
        return False, "Could not parse make command"
    scripts = _load_custom_scripts()
    if not tokens or scripts is None:
        return False, "make command cannot be validated without a worktree root"

    allowed = set(scripts.make_targets)
    for prev, tok in pairwise(tokens):
        if reason := _make_token_refusal(prev, tok, allowed):
            return False, reason
    # With no base targets every named target was refused above; this is bare `make`.
    if not allowed:
        return False, "make default target is not in the base commit's Makefile"
    return True, ""


def _extract_pkg_positionals(tokens: list[str]) -> tuple[list[str], str]:
    """Return the positional tokens and the first refused location option, if any."""
    positionals: list[str] = []
    err = ""
    i = 1
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith("-"):
            opt = tok.split("=", 1)[0]
            if opt in _REFUSED_PKG_OPTS and not err:
                err = f"option '{opt}' is not allowed when running a script"
            i += 2 if opt in _PKG_VALUE_OPTS and "=" not in tok else 1
            continue
        positionals.append(tok)
        i += 1
    return positionals, err


def _worktree_scripts() -> set[str]:
    """Script names in the working tree's package.json (what a runner would run)."""
    try:
        data = json.loads(
            (Path(get_worktree_root() or "") / "package.json").read_text("utf-8")
        )
        return set(data["scripts"])
    except (OSError, ValueError, KeyError, TypeError):
        return set()


def _target_script(tool: str, positionals: list[str]) -> str | None:
    if not positionals:
        return None
    sub = positionals[0]
    arg = positionals[1] if len(positionals) > 1 else None
    # `bun test` is bun's own runner, not the script, so bun skips these.
    if tool != "bun":
        if sub in _RUN_SUBCOMMANDS:
            return arg
        if sub in _TEST_SUBCOMMANDS:
            return "test"
        if sub in _LIFECYCLE_SUBCOMMANDS:
            return sub
    # yarn, pnpm and bun run `<tool> <script>` without `run`; `bun run x` runs
    # a script if one exists, else a file or binary.
    name = arg if tool == "bun" and sub == "run" and arg else sub
    return name if tool != "npm" and name in _worktree_scripts() else None


def validate_package_script_command(seg: str) -> ValidationResult:
    """Validate that npm/yarn/pnpm/bun only run scripts from the base commit's package.json."""
    try:
        tokens = shlex.split(seg)
    except ValueError:
        return False, "Could not parse package script command"
    scripts = _load_custom_scripts()
    if not tokens or scripts is None:
        return False, "package command cannot be validated without a worktree root"

    positionals, err = _extract_pkg_positionals(tokens)
    if positionals and positionals[0] in _REFUSED_PKG_SUBCOMMANDS:
        return False, f"subcommand '{positionals[0]}' is not allowed"

    script = _target_script(Path(tokens[0]).name, positionals)
    if script is None:
        return True, ""
    # Location options only matter when a script runs (`npm install -w pkg` is fine).
    if not err and script not in scripts.npm_scripts:
        err = f"script '{script}' is not in the base commit's package.json"
    return not err, err
