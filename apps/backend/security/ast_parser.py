"""
Fail-closed shell command extraction (AST-based)
================================================

The legacy ``parser.extract_commands`` validates only the *leading* token of
each segment and falls back to a leading-word regex when it cannot tokenize a
string — so ``bash -c '<payload>'``, ``env CMD``, ``xargs sh -c '<payload>'``,
``find . -exec CMD ...`` and command substitution all slipped past the
allowlist (epic #318 / #321, finding C3).

This module replaces that with a real shell parser (``bashlex`` — a port of
bash's own grammar). It surfaces **every** command that the string would
execute, including:

- pipe targets, ``&&``/``||``/``;`` chains, and grouped/subshell commands,
- ``$(...)`` / backtick command substitution (bashlex recurses these for free),
- the payload of ``bash``/``sh``/``zsh -c '<payload>'`` (re-parsed recursively),
- the real command behind ``env [VAR=val]... CMD``, ``xargs [flags] CMD`` and
  the ``command``/``exec``/``timeout`` prefixes (``-c`` also as ``-lc``, ``-ec``),
- the command run by ``find ... -exec/-execdir/-ok CMD ... ;|+``.

Two principles, both **fail closed**:

1. If the string cannot be parsed (``bashlex`` raises), raise
   :class:`UnparseableCommand` — the caller blocks rather than guessing.
2. If an interpreter payload (``bash -c``, ``xargs CMD``) is not a static
   literal we can re-parse (e.g. ``bash -c "$DYNAMIC"``), raise
   :class:`UnparseableCommand` — we cannot validate what we cannot see.
"""

from __future__ import annotations

import os
import shlex
from collections.abc import Callable
from typing import Any

try:
    import bashlex
    import bashlex.errors

    _BASHLEX_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only in degraded installs
    _BASHLEX_AVAILABLE = False


class UnparseableCommand(Exception):
    """The command could not be safely parsed — callers must fail closed."""


# Interpreters whose ``-c`` argument is itself a command string to validate.
_SHELL_INTERPRETERS = {"bash", "sh", "zsh", "dash", "ksh"}

# ``find`` actions that run an external command built from following tokens.
_FIND_EXEC_ACTIONS = {"-exec", "-execdir", "-ok", "-okdir"}

# Recursion guard — a pathological deeply-nested string should fail closed,
# not blow the stack.
_MAX_DEPTH = 25


def is_available() -> bool:
    """True when the AST parser backend (``bashlex``) is importable."""
    return _BASHLEX_AVAILABLE


def _word_parts_are_literal(node) -> bool:
    """True when a bashlex word node has no command/parameter substitution —
    i.e. it is a static literal we can safely re-parse."""
    for part in getattr(node, "parts", []) or []:
        # commandsubstitution / parameter (variable) / arithmetic etc. mean the
        # final value is not knowable statically.
        if getattr(part, "kind", None) in (
            "commandsubstitution",
            "parameter",
            "arithmetic",
            "processsubstitution",
        ):
            return False
    return True


def _collect_command_nodes(node, out: list) -> None:
    """Walk the bashlex AST collecting every ``command`` node (recursing into
    pipelines, lists, compound commands and command substitutions)."""
    kind = getattr(node, "kind", None)
    if kind == "command":
        out.append(node)
    # Recurse structural children. bashlex nests substitutions inside word
    # ``parts``; pipelines/lists/compounds carry children under these attrs.
    for attr in ("parts", "list", "command", "body", "pipe"):
        child = getattr(node, attr, None)
        if child is None:
            continue
        for c in child if isinstance(child, list) else [child]:
            if hasattr(c, "kind"):
                _collect_command_nodes(c, out)


def _command_words(node) -> list:
    """The word nodes of a ``command`` node, in order."""
    return [p for p in getattr(node, "parts", []) if getattr(p, "kind", None) == "word"]


def _basename(word: str) -> str:
    return os.path.basename(word)


def _extract(
    command_string: str,
    depth: int,
    acc: list[tuple[str, str, bool]],
    *,
    dynamic_args: bool = False,
) -> None:
    try:
        trees = bashlex.parse(command_string)
    except (bashlex.errors.ParsingError, NotImplementedError, Exception) as exc:
        # Fail closed: anything we cannot parse is treated as a block.
        raise UnparseableCommand(f"could not parse command: {exc}") from exc

    cmd_nodes: list = []
    for tree in trees:
        _collect_command_nodes(tree, cmd_nodes)

    for node in cmd_nodes:
        words = _command_words(node)
        if not words:
            continue

        # bashlex makes real ``VAR=value`` prefixes assignment nodes, which
        # ``_command_words`` drops; a word with ``=`` left here is run (#1753).
        _emit(words, 0, depth, acc, dynamic_args=dynamic_args)


def _emit(
    words: list[Any],
    idx: int,
    depth: int,
    acc: list[tuple[str, str, bool]],
    *,
    dynamic_args: bool = False,
) -> None:
    """Record the command at ``words[idx]`` and recurse into its wrapper target."""
    if depth > _MAX_DEPTH:
        raise UnparseableCommand("command nesting too deep to validate")
    # ``$X``/``$(...)`` as the command word runs a program we cannot see (#1753).
    if not _word_parts_are_literal(words[idx]):
        raise UnparseableCommand(
            "command word is not a static literal; cannot validate"
        )
    tokens = [w.word for w in words]
    name = _basename(tokens[idx])
    acc.append((name, shlex.join(tokens[idx:]), dynamic_args))
    unwrap = _UNWRAPPERS.get(name)
    if unwrap is not None:
        unwrap(words, idx, depth, acc, dynamic_args=dynamic_args)


def _unwrap_dash_c(
    words: list[Any],
    idx: int,
    depth: int,
    acc: list[tuple[str, str, bool]],
    *,
    dynamic_args: bool,
) -> None:
    """``<shell> -c '<payload>'`` (also ``-lc``, ``-ec``) — recurse into the
    payload, fail closed if it is not a static literal."""
    tokens = [w.word for w in words]
    for j in range(idx + 1, len(tokens)):
        tok = tokens[j]
        if (
            tok.startswith("-")
            and not tok.startswith("--")
            and "c" in tok[1:]
            and j + 1 < len(tokens)
        ):
            payload_word = words[j + 1]
            if not _word_parts_are_literal(payload_word):
                raise UnparseableCommand(
                    "shell -c payload is not a static literal; cannot validate"
                )
            _extract(payload_word.word, depth + 1, acc, dynamic_args=dynamic_args)
            return


_ENV_VALUE_LONG = ("--unset", "--chdir", "--argv0")
_ENV_FLAG_LONG = frozenset(
    {
        "--ignore-environment",
        "--null",
        "--debug",
        "--default-signal",
        "--ignore-signal",
        "--block-signal",
        "--list-signal-handling",
    }
)


def _skip_env_opt(tokens: list[str], j: int) -> int:
    """Index after the env option at ``tokens[j]``; raises for ``-S`` forms
    and for options whose value cannot be told apart from the command."""
    tok = tokens[j]
    if tok.startswith("--s") or ("S" in tok[1:] and not tok.startswith("--")):
        raise UnparseableCommand("env -S/--split-string cannot be validated")
    if tok.startswith("--"):
        name = tok.split("=", 1)[0]
        if name != "--" and any(v.startswith(name) for v in _ENV_VALUE_LONG):
            return j + 1 if "=" in tok else j + 2
        if name in _ENV_FLAG_LONG or tok == "--":
            return j + 1
        raise UnparseableCommand(f"env option {tok} cannot be validated")
    for i, ch in enumerate(tok[1:], 1):
        if ch in "uCa":  # value attached (-uNAME) or the next word
            return j + 1 if i < len(tok) - 1 else j + 2
    return j + 1


def _unwrap_env(
    words: list[Any],
    idx: int,
    depth: int,
    acc: list[tuple[str, str, bool]],
    *,
    dynamic_args: bool,
) -> None:
    """``env [-i] [VAR=val]... CMD args`` — the first non-flag, non-assignment
    token after ``env`` is the real command."""
    tokens = [w.word for w in words]
    j = idx + 1
    opts = True  # GNU env stops reading options at `--`, `-` or VAR=val
    while j < len(tokens):
        tok = tokens[j]
        if opts and tok in ("--", "-"):
            opts = False
            j += 1
            continue
        if opts and tok.startswith("-"):
            j = _skip_env_opt(tokens, j)
            continue
        if "=" in tok and not tok.startswith("="):
            opts = False
            j += 1
            continue
        _emit(words, j, depth + 1, acc, dynamic_args=dynamic_args)
        return


_XARGS_VALUE_SHORT = "InPdEsLa"  # value attached or the next word
_XARGS_ATTACHED_SHORT = "iel"  # optional value, attached only
_XARGS_FLAG_LONG = frozenset(
    {
        "--null",
        "--open-tty",
        "--exit",
        "--no-run-if-empty",
        "--verbose",
        "--interactive",
        "--show-limits",
        "--help",
        "--version",
        "--eof",
        "--replace",
        "--max-lines",
    }
)


def _skip_xargs_opt(tok: str, j: int) -> int:
    """Index after the xargs option ``tok`` at ``j``; raises when its value
    handling is ambiguous."""
    if tok.startswith("--"):
        if "=" in tok or tok in _XARGS_FLAG_LONG or tok == "--":
            return j + 1
        raise UnparseableCommand(f"xargs option {tok} cannot be validated")
    if tok[1:2] in _XARGS_VALUE_SHORT and len(tok) > 1:
        return j + 1 if tok[2:] else j + 2
    if tok[1:2] not in _XARGS_ATTACHED_SHORT and any(
        c in _XARGS_VALUE_SHORT + _XARGS_ATTACHED_SHORT for c in tok[2:]
    ):
        raise UnparseableCommand(f"xargs options {tok} cannot be validated")
    return j + 1


def _unwrap_xargs(
    words: list[Any],
    idx: int,
    depth: int,
    acc: list[tuple[str, str, bool]],
    *,
    dynamic_args: bool,
) -> None:
    """``xargs [flags] CMD args`` — the first non-flag token (skipping option
    values) after ``xargs`` is the real command."""
    del dynamic_args  # xargs always adds arguments we cannot see
    tokens = [w.word for w in words]
    j = idx + 1
    while j < len(tokens):
        tok = tokens[j]
        if tok == "--":  # the next word is the command, even if it starts with -
            j += 1
            break
        if tok.startswith("-") and tok != "-":
            j = _skip_xargs_opt(tok, j)
            continue
        break
    if j < len(tokens):
        _emit(words, j, depth + 1, acc, dynamic_args=True)
    # ``xargs`` with no explicit command defaults to running ``echo`` — benign.


def _unwrap_find_exec(
    words: list[Any],
    idx: int,
    depth: int,
    acc: list[tuple[str, str, bool]],
    *,
    dynamic_args: bool,
) -> None:
    """``find ... -exec/-execdir/-ok CMD ... ;|+`` — the token after the action
    is the command find runs for each match."""
    del dynamic_args  # find always substitutes matched paths
    tokens = [w.word for w in words]
    for j in range(idx + 1, len(tokens) - 1):
        if tokens[j] in _FIND_EXEC_ACTIONS:
            _emit(words, j + 1, depth + 1, acc, dynamic_args=True)


def _skip_command_opts(tokens: list[str], j: int) -> int | None:
    """``command [-p] CMD`` — None for ``-v``/``-V`` (lookup, runs nothing)."""
    while j < len(tokens) and tokens[j].startswith("-") and tokens[j] != "-":
        tok = tokens[j]
        j += 1
        if tok == "--":
            break
        if not tok.startswith("--") and {"v", "V"} & set(tok[1:]):
            return None
    return j


def _skip_exec_opts(tokens: list[str], j: int) -> int | None:
    """``exec [-cl] [-a NAME] CMD`` — ``-a`` consumes its value."""
    while j < len(tokens) and tokens[j].startswith("-") and tokens[j] != "-":
        tok = tokens[j]
        # ``a`` last in the group takes the next word; ``-aNAME`` is attached.
        takes_next = not tok.startswith("--") and tok.find("a") == len(tok) - 1
        j += 2 if takes_next else 1
        if tok == "--":
            break
    return j


def _skip_timeout_opts(tokens: list[str], j: int) -> int | None:
    """``timeout [opts] DURATION CMD`` — ``-s``/``-k`` take a value."""
    while j < len(tokens) and tokens[j].startswith("-"):
        tok = tokens[j]
        j += 2 if tok in ("-s", "--signal", "-k", "--kill-after") else 1
    return j + 1  # DURATION


_PREFIX_OPT_SKIPPERS: dict[str, Callable[[list[str], int], int | None]] = {
    "command": _skip_command_opts,
    "exec": _skip_exec_opts,
    "timeout": _skip_timeout_opts,
}


def _unwrap_prefix(
    words: list[Any],
    idx: int,
    depth: int,
    acc: list[tuple[str, str, bool]],
    *,
    dynamic_args: bool,
) -> None:
    """``command``/``exec``/``timeout`` run the command that follows their options."""
    tokens = [w.word for w in words]
    j = _PREFIX_OPT_SKIPPERS[_basename(tokens[idx])](tokens, idx + 1)
    if j is None or j >= len(tokens):
        return
    _emit(words, j, depth + 1, acc, dynamic_args=dynamic_args)


_UNWRAPPERS: dict[str, Callable[..., None]] = {
    **dict.fromkeys(_SHELL_INTERPRETERS, _unwrap_dash_c),
    "env": _unwrap_env,
    "xargs": _unwrap_xargs,
    "find": _unwrap_find_exec,
    **dict.fromkeys(_PREFIX_OPT_SKIPPERS, _unwrap_prefix),
}


def extract_command_segments_ast(
    command_string: str,
) -> list[tuple[str, str, bool]]:
    """Return ``(name, segment, dynamic_args)`` for every command the string
    would execute, recursively unwrapping wrappers and substitutions.

    ``segment`` starts at the command word; ``dynamic_args`` is True when
    xargs or ``find -exec`` appends arguments the validators cannot see.

    Raises:
        UnparseableCommand: if the string cannot be parsed, or an interpreter
            payload is dynamic — the caller must block (fail closed).
    """
    if not _BASHLEX_AVAILABLE:  # pragma: no cover
        raise UnparseableCommand("bashlex is not installed")
    if not command_string or not command_string.strip():
        return []
    acc: list[tuple[str, str, bool]] = []
    _extract(command_string, 0, acc)
    return acc


def extract_commands_ast(command_string: str) -> list[str]:
    """Return every command name the string would execute, recursively
    unwrapping interpreters and substitutions.

    Raises:
        UnparseableCommand: if the string cannot be parsed, or an interpreter
            payload is dynamic — the caller must block (fail closed).
    """
    return [n for n, _, _ in extract_command_segments_ast(command_string)]
