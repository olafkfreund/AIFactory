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
import re
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


# Allow list of word shapes (#1753): bash expands anything else (globs,
# braces, ``~``, ``$``, escapes) into words we cannot see statically.
_SAFE_UNQUOTED = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_./=:@%+,-"
)
_DQ_UNSAFE = frozenset("$`\\")

_SAFE_GIT_KEYS = frozenset(
    {
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_AUTHOR_DATE",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
        "GIT_COMMITTER_DATE",
        "GIT_TERMINAL_PROMPT",
        "GIT_OPTIONAL_LOCKS",
        "GIT_FLUSH",
        "GIT_MERGE_AUTOEDIT",
        "GIT_LFS_SKIP_SMUDGE",
    }
)
_PROGRAM_KEYS = frozenset(
    {"GIT_PAGER", "GIT_EDITOR", "GIT_SEQUENCE_EDITOR", "PAGER", "EDITOR", "VISUAL"}
)
_NOOP_PROGRAMS = frozenset({"", "cat", "true", ":"})
_REFUSED_KEYS = frozenset({"SSH_ASKPASS"})
_VAR_SETTERS = frozenset(
    {
        "export",
        "read",
        "let",
        "unset",
        "declare",
        "typeset",
        "local",
        "readonly",
        "mapfile",
        "readarray",
        "getopts",
    }
)
_NAMEREF_SETTERS = frozenset({"declare", "typeset", "local"})
_NAME_ASSIGN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\+?=")


def _env_key_refusal(assignment: str) -> None:
    """Refuse setting or unsetting env keys that change git config or what git runs."""
    raw_name, eq, value = assignment.partition("=")
    name = raw_name.removesuffix("+")
    if name in _PROGRAM_KEYS:
        allowed = not eq or value in _NOOP_PROGRAMS
    else:
        allowed = name not in _REFUSED_KEYS and (
            not name.startswith("GIT_") or name in _SAFE_GIT_KEYS
        )
    if not allowed:
        raise UnparseableCommand(
            f"setting {name} can change git config or what git runs; not permitted"
        )


def _static_shape(raw: str) -> bool:
    """True when the raw word expands to exactly itself: safe unquoted
    characters, backslash escapes, single-quoted text, double-quoted text
    without ``$``, backtick or backslash, and ``{}`` (never expands). A lone ``[``/``]`` is the test
    command."""
    if raw in ("[", "]"):
        return True
    i = 0
    while i < len(raw):
        c = raw[i]
        if c in ("'", '"'):
            end = raw.find(c, i + 1)
            if end < 0 or (c == '"' and _DQ_UNSAFE & set(raw[i + 1 : end])):
                return False
            i = end + 1
        # ``{}`` never expands; an escape is one literal char (``\;``).
        elif raw.startswith("{}", i) or (c == "\\" and i + 1 < len(raw)):
            i += 2
        elif c in _SAFE_UNQUOTED:
            i += 1
        else:
            return False
    return True


def _word_parts_are_literal(node) -> bool:
    """True when a bashlex word node has no command/parameter substitution
    and an allow-listed shape (``_static_shape``) — i.e. it is a static literal
    we can safely re-parse."""
    raw = getattr(node, "raw", None)  # source text, quotes kept (set in _extract)
    if raw is not None and not _static_shape(raw):
        return False
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


def _require_literal(words: list[Any], start: int, stop: int) -> None:
    """Raise unless ``words[start:stop]`` are all static literals. A wrapper's
    skipped flags/values word-split at run time, so ``$X`` there can become
    the command it runs (#1753)."""
    if not all(_word_parts_are_literal(w) for w in words[start:stop]):
        raise UnparseableCommand(
            "wrapper argument is not a static literal; cannot validate"
        )


def _check_var_word(w: Any) -> None:
    """Require a static variable name/assignment word and check its key."""
    if not (_static_shape(w.raw) or _NAME_ASSIGN.match(w.raw)):
        raise UnparseableCommand(
            "variable name is not a static literal; cannot validate"
        )
    _env_key_refusal(w.word)


def _check_var_setter(words: list[Any], idx: int) -> None:
    """Validate options and variable names for shell builtins that set/unset vars."""
    setter = _basename(words[idx].word)
    for k in range(idx + 1, len(words)):
        w = words[k]
        if w.word.startswith("-"):
            _require_literal(words, k, k + 1)
            if (
                setter in _NAMEREF_SETTERS
                and not w.word.startswith("--")
                and "n" in w.word[1:]
            ):
                raise UnparseableCommand("nameref is not permitted")
        else:
            _check_var_word(w)


def _check_printf(words: list[Any], idx: int) -> None:
    """Validate ``printf -v NAME`` / ``printf -vNAME`` variable targets."""
    k = idx + 1
    while k < len(words):
        tok = words[k].word
        if tok == "--" or not tok.startswith("-") or tok == "-":
            break
        _require_literal(words, k, k + 1)
        if tok == "-v":
            if k + 1 >= len(words):
                raise UnparseableCommand("printf -v missing variable name")
            _check_var_word(words[k + 1])
            k += 2
        elif tok.startswith("-v"):
            if not _static_shape(words[k].raw):
                raise UnparseableCommand(
                    "variable name is not a static literal; cannot validate"
                )
            _env_key_refusal(tok[2:])
            k += 1
        else:
            k += 1


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
        for part in getattr(node, "parts", []):
            if getattr(part, "kind", None) == "assignment":
                _env_key_refusal(part.word)
        words = _command_words(node)
        if not words:
            continue
        for w in words:  # ``.word`` drops quotes; the glob check needs them
            w.raw = command_string[w.pos[0] : w.pos[1]]

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
    if name in _VAR_SETTERS:
        _check_var_setter(words, idx)
    elif name == "printf":
        _check_printf(words, idx)
    unwrap = _UNWRAPPERS.get(name)
    if unwrap is not None:
        unwrap(words, idx, depth, acc, dynamic_args=dynamic_args)


# Shell flags (``-x``/``+x``) that take no value and do not read stdin.
_SHELL_FLAGS = frozenset("eulxv")
# ``-c`` payloads are re-parsed with bashlex, so only POSIX/bash grammar
# shells qualify (zsh ``=cmd`` and ksh differ).
_DASH_C_SHELLS = frozenset({"bash", "sh", "dash"})
# Cheap refusals only. A script operand is trusted file content (like
# ``source``, ``BASH_ENV`` and ``-l`` profiles), not a parser boundary.
_STDIN_SCRIPT = re.compile(r"-|/dev/stdin|/dev/fd/.*|/proc/[^/]+/fd/.*")


def _unwrap_dash_c(
    words: list[Any],
    idx: int,
    depth: int,
    acc: list[tuple[str, str, bool]],
    *,
    dynamic_args: bool,
) -> None:
    """Accept only ``<shell> [-+flags]... -c [-+flags]... '<payload>' [args]``
    (recurse into the payload; bash/sh/dash only) or
    ``<shell> [-+flags]... <script> [args]``. Flags come from
    ``_SHELL_FLAGS``; any other option fails closed. As in bash, the operand
    is the first non-option word, wherever ``-c`` appears."""
    tokens = [w.word for w in words]
    dash_c = False
    j = idx + 1
    while j < len(tokens):
        _require_literal(words, j, j + 1)
        tok = tokens[j]
        if tok == "--":
            j += 1
            break
        if tok[:1] not in ("-", "+") or tok == "-":
            break
        flags = set(tok[1:])
        if tok[0] == "-" and "c" in flags:
            dash_c = True
            flags.discard("c")
        if not flags <= _SHELL_FLAGS or not tok[1:]:  # a bare `+`
            raise UnparseableCommand(f"shell option {tok} cannot be validated")
        j += 1
    if j >= len(tokens):
        raise UnparseableCommand("shell has no command or script; cannot validate")
    _require_literal(words, j, j + 1)
    if dash_c:
        if _basename(tokens[idx]) not in _DASH_C_SHELLS:
            raise UnparseableCommand(f"{tokens[idx]} -c cannot be validated")
        _extract(tokens[j], depth + 1, acc, dynamic_args=dynamic_args)
    elif _STDIN_SCRIPT.fullmatch(tokens[j]):
        raise UnparseableCommand(f"shell operand {tokens[j]} cannot be validated")


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


def _check_env_opt(tokens: list[str], j: int) -> None:
    """Refuse ``env`` options that clear the environment or unset protected keys."""
    tok = tokens[j]
    if tok.startswith("--"):
        opt, eq, val = tok.partition("=")
        if opt != "--" and "--ignore-environment".startswith(opt):
            raise UnparseableCommand("env --ignore-environment clears environment pins")
        if opt != "--" and "--unset".startswith(opt):
            if not eq and j + 1 >= len(tokens):
                raise UnparseableCommand("env --unset missing variable name")
            _env_key_refusal(val if eq else tokens[j + 1])
        return
    for i, ch in enumerate(tok[1:], 1):
        if ch == "i":
            raise UnparseableCommand("env -i clears environment pins")
        if ch in "uCa":
            if ch == "u":
                if i == len(tok) - 1 and j + 1 >= len(tokens):
                    raise UnparseableCommand("env -u missing variable name")
                _env_key_refusal(tok[i + 1 :] if i < len(tok) - 1 else tokens[j + 1])
            return


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
            if tok == "-":
                raise UnparseableCommand("env - clears environment pins")
            opts = False
            j += 1
            continue
        if opts and tok.startswith("-"):
            _check_env_opt(tokens, j)
            j = _skip_env_opt(tokens, j)
            continue
        if "=" in tok and not tok.startswith("="):
            _env_key_refusal(tok)
            opts = False
            j += 1
            continue
        break
    _require_literal(words, idx + 1, j)
    if j < len(tokens):
        _emit(words, j, depth + 1, acc, dynamic_args=dynamic_args)


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
    _require_literal(words, idx + 1, j)
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
    # Any word can split into a new ``-exec`` action, so all must be literal.
    _require_literal(words, idx + 1, len(words))
    tokens = [w.word for w in words]
    for j in range(idx + 1, len(tokens) - 1):
        if tokens[j] in _FIND_EXEC_ACTIONS:
            _emit(words, j + 1, depth + 1, acc, dynamic_args=True)


def _is_opt(tok: str) -> bool:
    return tok.startswith("-") and tok != "-"


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
        if not tok.startswith("--") and "c" in tok[1:]:
            raise UnparseableCommand("exec -c clears environment pins")
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
    opts_end = next(
        (k for k in range(idx + 1, len(tokens)) if not _is_opt(tokens[k])),
        len(tokens),
    )
    # Before the ``-v`` exemption: ``-$X`` could hide or fake the ``v``.
    _require_literal(words, idx + 1, opts_end)
    j = _PREFIX_OPT_SKIPPERS[_basename(tokens[idx])](tokens, idx + 1)
    if j is None:  # ``command -v``: a lookup, runs nothing
        return
    _require_literal(words, idx + 1, j)
    if j >= len(tokens):
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
