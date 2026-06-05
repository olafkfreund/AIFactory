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
- the real command behind ``env [VAR=val]... CMD`` and ``xargs [flags] CMD``,
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


def _extract(command_string: str, depth: int, acc: list[str]) -> None:
    if depth > _MAX_DEPTH:
        raise UnparseableCommand("command nesting too deep to validate")

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

        # Skip leading ``VAR=value`` assignment words to find the real command.
        idx = 0
        while (
            idx < len(words)
            and "=" in words[idx].word
            and not words[idx].word.startswith("=")
        ):
            idx += 1
        if idx >= len(words):
            continue

        tokens = [w.word for w in words]
        # If the command word is itself a substitution (e.g. a bare
        # ``$(...)`` / backtick command), bashlex already collected its inner
        # commands separately — don't emit the substitution text as a name.
        if not _word_parts_are_literal(words[idx]):
            continue
        name = _basename(tokens[idx])
        acc.append(name)

        # --- Unwrap interpreters whose payload is itself a command. ---
        if name in _SHELL_INTERPRETERS:
            _unwrap_dash_c(words, tokens, idx, depth, acc)
        elif name == "env":
            _unwrap_env(words, tokens, idx, depth, acc)
        elif name == "xargs":
            _unwrap_xargs(words, tokens, idx, depth, acc)
        elif name == "find":
            _unwrap_find_exec(words, tokens, idx, depth, acc)


def _unwrap_dash_c(words, tokens, idx, depth, acc) -> None:
    """``<shell> -c '<payload>'`` — recurse into the payload, fail closed if it
    is not a static literal."""
    for j in range(idx + 1, len(tokens)):
        if tokens[j] == "-c" and j + 1 < len(tokens):
            payload_word = words[j + 1]
            if not _word_parts_are_literal(payload_word):
                raise UnparseableCommand(
                    "shell -c payload is not a static literal; cannot validate"
                )
            _extract(payload_word.word, depth + 1, acc)
            return
        # A bare ``-c`` with no payload, or other flags, are ignored.


def _unwrap_env(words, tokens, idx, depth, acc) -> None:
    """``env [-i] [VAR=val]... CMD args`` — the first non-flag, non-assignment
    token after ``env`` is the real command."""
    for j in range(idx + 1, len(tokens)):
        tok = tokens[j]
        if tok.startswith("-"):
            continue
        if "=" in tok and not tok.startswith("="):
            continue
        acc.append(_basename(tok))
        # If env launches a shell, recurse into a following -c payload.
        if _basename(tok) in _SHELL_INTERPRETERS:
            _unwrap_dash_c(words, tokens, j, depth, acc)
        return


def _unwrap_xargs(words, tokens, idx, depth, acc) -> None:
    """``xargs [flags] CMD args`` — the first non-flag token (skipping option
    values) after ``xargs`` is the real command."""
    j = idx + 1
    while j < len(tokens):
        tok = tokens[j]
        if tok.startswith("-"):
            # Options that take a value: skip the value too.
            if tok in ("-I", "-i", "-n", "-P", "-d", "-E", "-s", "-L", "-a"):
                j += 2
                continue
            j += 1
            continue
        acc.append(_basename(tok))
        if _basename(tok) in _SHELL_INTERPRETERS:
            _unwrap_dash_c(words, tokens, j, depth, acc)
        return
    # ``xargs`` with no explicit command defaults to running ``echo`` — benign.


def _unwrap_find_exec(words, tokens, idx, depth, acc) -> None:
    """``find ... -exec/-execdir/-ok CMD ... ;|+`` — the token after the action
    is the command find runs for each match."""
    for j in range(idx + 1, len(tokens) - 1):
        if tokens[j] in _FIND_EXEC_ACTIONS:
            target = _basename(tokens[j + 1])
            acc.append(target)
            if target in _SHELL_INTERPRETERS:
                _unwrap_dash_c(words, tokens, j + 1, depth, acc)


def extract_commands_ast(command_string: str) -> list[str]:
    """Return every command name the string would execute, recursively
    unwrapping interpreters and substitutions.

    Raises:
        UnparseableCommand: if the string cannot be parsed, or an interpreter
            payload is dynamic — the caller must block (fail closed).
    """
    if not _BASHLEX_AVAILABLE:  # pragma: no cover
        raise UnparseableCommand("bashlex is not installed")
    if not command_string or not command_string.strip():
        return []
    acc: list[str] = []
    _extract(command_string, 0, acc)
    return acc
