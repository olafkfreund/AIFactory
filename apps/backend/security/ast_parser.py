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
_UNKNOWN_VALUE_SETTERS = frozenset({"read", "mapfile", "readarray", "getopts"})
_NAME_ASSIGN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\+?=")
# ``${NAME:=v}`` / ``${NAME=v}`` assign NAME; scanned on the raw string so
# quoted words and heredoc bodies are covered too.
_DEFAULT_ASSIGN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*):?=")
# bashlex rejects the ``time`` keyword; ``\time`` is the plain word, which
# ``_unwrap_prefix`` already checks (#1809).
_TIME_KEYWORD = re.compile(r"(^|[;&|({!\n])([ \t]*)time(?=[ \t\n;&|)]|\Z)")
# bashlex matches heredoc lines against the delimiter with its quotes (#1809).
_QUOTED_HEREDOC = re.compile(
    r"(?<!<)(<<-?)(?!<)([ \t]*)"
    r"((?:[A-Za-z0-9_.-]|'[A-Za-z0-9_.-]*'|\"[A-Za-z0-9_.-]*\"|\\[A-Za-z0-9_.-])+)"
    r"(?=[\s;&|<>()]|\Z)"
)


def _unquote_heredocs(text: str) -> tuple[str, set[int]]:
    """Unquote heredoc delimiters; return the text and each delimiter's offset."""
    out: list[str] = []
    offsets: set[int] = set()
    last = 0
    for m in _QUOTED_HEREDOC.finditer(text):
        word = m.group(3)
        if not any(c in word for c in "'\"\\"):
            continue
        out.append(text[last : m.start()] + m.group(1) + m.group(2))
        offsets.add(sum(map(len, out)))
        out.append(re.sub(r"['\"\\]", "", word))
        last = m.end()
    out.append(text[last:])
    return "".join(out), offsets


def _blank_quoted_bodies(node: Any, offsets: set[int], found: set[int]) -> None:
    """A quoted heredoc body is never expanded, so it is data: drop it."""
    if getattr(node, "kind", None) == "redirect" and node.type in ("<<", "<<-"):
        start = getattr(getattr(node, "output", None), "pos", (None,))[0]
        if start in offsets and getattr(node, "heredoc", None) is not None:
            found.add(start)
            node.heredoc.value = ""
    for val in vars(node).values():
        for c in val if isinstance(val, list) else (val,):
            if hasattr(c, "kind"):
                _blank_quoted_bodies(c, offsets, found)


def _parse(text: str) -> list[Any]:
    try:
        trees: list[Any] = bashlex.parse(text)
    except (bashlex.errors.ParsingError, NotImplementedError, Exception) as exc:
        # Fail closed: anything we cannot parse is treated as a block.
        raise UnparseableCommand(f"could not parse command: {exc}") from exc
    return trees


def _env_key_refusal(assignment: str) -> None:
    """Refuse setting or unsetting env keys that change git config or what git runs."""
    raw_name, eq, value = assignment.partition("=")
    name = raw_name.removesuffix("+")
    if "[" in name:
        # Bash evaluates a subscript as arithmetic, which runs any ``$(…)``.
        raise UnparseableCommand(f"array subscript in {name} is not permitted")
    if name.startswith("LD_"):
        allowed = False
    elif name in _PROGRAM_KEYS:
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


def _check_var_word(w: Any, *, unknown_value: bool = False) -> None:
    """Require a static variable name/assignment word and check its key."""
    if not (_static_shape(w.raw) or _NAME_ASSIGN.match(w.raw)):
        raise UnparseableCommand(
            "variable name is not a static literal; cannot validate"
        )
    _env_key_refusal(f"{w.word}=?" if unknown_value and "=" not in w.word else w.word)


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
            # An integer variable's value is evaluated as arithmetic, which
            # can assign any variable: ``declare -i x=GIT_CONFIG_COUNT=0``.
            if (
                setter in _NAMEREF_SETTERS
                and not w.word.startswith("--")
                and "i" in w.word[1:]
            ):
                raise UnparseableCommand("integer attribute is not permitted")
            if setter in ("mapfile", "readarray") and "C" in w.word[1:]:
                raise UnparseableCommand("mapfile callback is not permitted")
        else:
            _check_var_word(w, unknown_value=setter in _UNKNOWN_VALUE_SETTERS)


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


# Inside ``${…}`` and heredoc bodies (text bashlex leaves unparsed), refuse
# command/arithmetic substitution and ``${…}`` forms that evaluate text (#1797).
_RUNS_COMMAND = re.compile(r"\$\(|`|\$\[")
_PARAM_RE = re.compile(
    r"([#!]?)([A-Za-z_][A-Za-z0-9_]*|[0-9]+|[@*#?$!-])"
    r"(?:\[(-?[0-9]+|[@*])\])?(?:\Z|(?=[-*@#%/^,:?+=]))(.*)",
    re.DOTALL,
)
_OFFSET_RE = re.compile(r":\s*-?[0-9]+(?::\s*-?[0-9]+)?")


def _check_parameter(value: str, *, heredoc: bool = False) -> None:
    if _RUNS_COMMAND.search(value):
        raise UnparseableCommand(
            "command substitution inside ${…} or a heredoc body cannot be validated"
        )
    if not heredoc:
        m = _PARAM_RE.fullmatch(value)
        if not m:
            raise UnparseableCommand(
                f"parameter expansion ${{{value}}} cannot be validated"
            )
        prefix, name, sub, rest = m.groups()
        if (
            (
                prefix == "!"
                and (
                    not name.isidentifier()
                    or (sub, rest)
                    not in ((None, "*"), (None, "@"), ("@", ""), ("*", ""))
                )
            )
            or (prefix != "!" and rest.startswith("*"))
            or rest.startswith(("@P", "@E", "@A"))
            or (
                rest.startswith(":")
                and rest[:2] not in (":-", ":=", ":?", ":+")
                and not _OFFSET_RE.fullmatch(rest)
            )
        ):
            raise UnparseableCommand(
                f"parameter expansion ${{{value}}} cannot be validated"
            )
        value = rest
    i = value.find("${")
    while i >= 0:
        depth = 1
        j = i + 2
        while j < len(value) and depth:
            if value[j] == "\\":
                j += 2
                continue
            if value[j] == "{":
                depth += 1
            elif value[j] == "}":
                depth -= 1
            j += 1
        if depth:
            raise UnparseableCommand("unclosed ${ in parameter or heredoc")
        _check_parameter(value[i + 2 : j - 1])
        i = value.find("${", j)


def _collect_command_nodes(
    node: Any, out: list[Any], seen: set[int] | None = None
) -> None:
    """Walk every child node of the bashlex AST collecting ``command`` nodes,
    so redirect targets and any other attribute are covered (#1783)."""
    visited = seen if seen is not None else set()
    if id(node) in visited:
        return
    visited.add(id(node))
    kind = getattr(node, "kind", None)
    if kind == "for":
        _env_key_refusal(f"{node.parts[1].word}=?")
    if kind in ("parameter", "heredoc"):
        _check_parameter(getattr(node, "value", ""), heredoc=(kind == "heredoc"))
    if kind == "command":
        out.append(node)
    for val in vars(node).values():
        for c in val if isinstance(val, list) else (val,):
            if hasattr(c, "kind"):
                _collect_command_nodes(c, out, visited)


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
    timed = _TIME_KEYWORD.sub(r"\1\2\\time", command_string)
    text, offsets = _unquote_heredocs(timed)
    try:
        trees = _parse(text)
        found: set[int] = set()
        for tree in trees:
            _blank_quoted_bodies(tree, offsets, found)
        if found != offsets:  # a rewritten ``<<`` was not a real heredoc
            raise UnparseableCommand("heredoc rewrite not confirmed")
    except UnparseableCommand:
        if not offsets:
            raise
        text, trees = timed, _parse(timed)
    command_string = text  # node positions refer to the parsed text

    for m in _DEFAULT_ASSIGN.finditer(command_string):
        _env_key_refusal(f"{m.group(1)}=?")

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


def _check_builtin(words: list[Any], idx: int, name: str) -> None:
    """Refuse builtins that set variables or evaluate arithmetic unsafely."""
    tokens = [w.word for w in words]
    if name == "let":
        raise UnparseableCommand("let arithmetic cannot be validated")
    if name in ("test", "["):
        # ``-v NAME`` evaluates a subscript in NAME as arithmetic.
        for k in range(idx + 1, len(words) - 1):
            if tokens[k] == "-v":
                _check_var_word(words[k + 1])
    if name == "set":
        # A dynamic word could expand to ``-a``; after ``--`` words are positional.
        for k in range(idx + 1, len(words)):
            if tokens[k] == "--":
                break
            _require_literal(words, k, k + 1)
            if tokens[k] == "allexport" or (
                tokens[k].startswith("-") and "a" in tokens[k][1:]
            ):
                raise UnparseableCommand("set allexport is not permitted")
    if name in _VAR_SETTERS:
        _check_var_setter(words, idx)
    elif name == "printf":
        _check_printf(words, idx)


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
    _check_builtin(words, idx, name)
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
_STDIN_SCRIPT = re.compile(r"-|/dev/.*|/proc/.*")


def _is_stdin_path(tok: str) -> bool:
    return bool(_STDIN_SCRIPT.fullmatch(re.sub(r"^/+", "/", os.path.normpath(tok))))


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
    elif _is_stdin_path(tokens[j]):
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


_TIME_VALUE_OPTS = frozenset({"-f", "-o", "--format", "--output"})
_TIME_FLAG_OPTS = frozenset(
    {
        "-p",
        "-a",
        "-v",
        "-q",
        "--portability",
        "--append",
        "--verbose",
        "--quiet",
    }
)


def _skip_time_opts(tokens: list[str], j: int) -> int | None:
    """``time [opts] CMD`` — GNU/POSIX ``time`` options."""
    while j < len(tokens) and tokens[j].startswith("-"):
        tok = tokens[j]
        if tok == "--":
            return j + 1
        if tok in _TIME_VALUE_OPTS:
            j += 2
        elif tok in _TIME_FLAG_OPTS or tok.startswith(("--format=", "--output=")):
            j += 1
        else:
            raise UnparseableCommand(f"time option {tok} cannot be validated")
    return j


_PREFIX_OPT_SKIPPERS: dict[str, Callable[[list[str], int], int | None]] = {
    "command": _skip_command_opts,
    "exec": _skip_exec_opts,
    "timeout": _skip_timeout_opts,
    "time": _skip_time_opts,
}


def _unwrap_prefix(
    words: list[Any],
    idx: int,
    depth: int,
    acc: list[tuple[str, str, bool]],
    *,
    dynamic_args: bool,
) -> None:
    """``command``/``exec``/``timeout``/``time`` run the command after their options."""
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


def _unwrap_source(
    words: list[Any],
    idx: int,
    depth: int,
    acc: list[tuple[str, str, bool]],
    *,
    dynamic_args: bool,
) -> None:
    """``source``/``.`` ``<script> [args]`` — the script path must be a static file."""
    del depth, acc, dynamic_args
    if idx + 1 < len(words):
        _require_literal(words, idx + 1, idx + 2)
        script = words[idx + 1].word
        if script.startswith("-") or _is_stdin_path(script):
            raise UnparseableCommand(f"source operand {script} cannot be validated")


_RUNNER_FLAGS = frozenset(
    {
        "--no",
        "-q",
        "--quiet",
        "--ignore-existing",
        "--no-install",
        "--include-workspace-root",
        "--workspaces",
        "-ws",
    }
)
_RUNNER_VALUE_OPTS = frozenset({"-w", "--workspace"})
_RUNNER_SUBCOMMANDS: dict[str, tuple[tuple[str, ...], ...]] = {
    "pnpm": (("exec",), ("dlx",)),
    "yarn": (("exec",), ("dlx",)),
    "bun": (("x",), ("run",)),
    "uv": (("tool", "run"), ("run",)),
    "pipx": (("run",),),
}
_RUNNER_GLOBALS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "pnpm": (
        frozenset(
            [
                "-r",
                "--recursive",
                "-w",
                "--workspace-root",
                "-s",
                "--silent",
                "--parallel",
                "--stream",
            ]
        ),
        frozenset(["--filter", "-F", "-C", "--dir"]),
    ),
    "yarn": (frozenset(["-s", "--silent"]), frozenset({"--cwd"})),
    "bun": (frozenset(["--bun", "--silent"]), frozenset({"--cwd"})),
}
_YARN_FOREACH_OPTS = (
    frozenset(
        [
            "-A",
            "--all",
            "-R",
            "--recursive",
            "-W",
            "--worktree",
            "-p",
            "--parallel",
            "-i",
            "--interlaced",
            "-t",
            "--topological",
            "--topological-dev",
            "-v",
            "--verbose",
        ]
    ),
    frozenset(["--include", "--exclude", "--from", "-j", "--jobs"]),
)
_RUNNER_BUILTINS: dict[str, frozenset[str]] = {
    "pnpm": frozenset(
        [
            "add",
            "approve-builds",
            "audit",
            "bin",
            "c",
            "cache",
            "cat-file",
            "cat-index",
            "config",
            "create",
            "dedupe",
            "deploy",
            "doctor",
            "env",
            "fetch",
            "find-hash",
            "help",
            "i",
            "ignored-builds",
            "import",
            "init",
            "install",
            "install-test",
            "it",
            "la",
            "licenses",
            "link",
            "list",
            "ll",
            "ln",
            "ls",
            "outdated",
            "pack",
            "patch",
            "patch-commit",
            "patch-remove",
            "prune",
            "publish",
            "rb",
            "rebuild",
            "remove",
            "rm",
            "root",
            "run",
            "run-script",
            "self-update",
            "server",
            "setup",
            "start",
            "store",
            "t",
            "test",
            "tst",
            "un",
            "uninstall",
            "unlink",
            "up",
            "update",
            "upgrade",
            "why",
        ]
    ),
    "yarn": frozenset(
        [
            "add",
            "audit",
            "autoclean",
            "bin",
            "cache",
            "check",
            "config",
            "constraints",
            "create",
            "dedupe",
            "explain",
            "generate-lock-entry",
            "global",
            "help",
            "import",
            "info",
            "init",
            "install",
            "licenses",
            "link",
            "list",
            "login",
            "logout",
            "npm",
            "outdated",
            "owner",
            "pack",
            "patch",
            "patch-commit",
            "plugin",
            "policies",
            "publish",
            "rebuild",
            "remove",
            "run",
            "search",
            "set",
            "stage",
            "tag",
            "team",
            "test",
            "unlink",
            "unplug",
            "up",
            "upgrade",
            "upgrade-interactive",
            "version",
            "versions",
            "why",
            "workspaces",
        ]
    ),
    "bun": frozenset(
        [
            "a",
            "add",
            "audit",
            "build",
            "c",
            "completions",
            "create",
            "help",
            "i",
            "info",
            "init",
            "install",
            "link",
            "outdated",
            "patch",
            "patch-commit",
            "pm",
            "publish",
            "remove",
            "repl",
            "rm",
            "test",
            "unlink",
            "update",
            "upgrade",
            "why",
        ]
    ),
}
_NPM_GLOBAL_FLAGS = frozenset(
    {
        "-g",
        "--global",
        "-s",
        "--silent",
        "-q",
        "--quiet",
        "-d",
        "--verbose",
        "-y",
        "--yes",
        "-ws",
        "--workspaces",
        "--include-workspace-root",
        "-v",
        "--version",
        "-h",
        "--help",
    }
)
_NPM_GLOBAL_VALUE_OPTS = frozenset(
    {"-w", "--workspace", "--prefix", "--loglevel", "--registry"}
)
_NPM_SUBCOMMAND_OPTS = frozenset(
    {
        "--save",
        "-S",
        "--save-dev",
        "-D",
        "--save-exact",
        "-E",
        "--save-optional",
        "-O",
        "--save-peer",
        "--no-save",
        "--omit",
        "--include",
        "--production",
        "--only",
        "--legacy-peer-deps",
        "--strict-peer-deps",
        "--ignore-scripts",
        "--no-audit",
        "--no-fund",
        "--force",
        "-f",
        "--dry-run",
        "--json",
        "--parseable",
        "--depth",
        "--all",
        "-a",
        "--audit-level",
        "--if-present",
        "--long",
        "-l",
        "--prefer-offline",
        "--prefer-online",
        "--offline",
        "--no-package-lock",
        "--package-lock-only",
        "--frozen-lockfile",
        "--no-git-tag-version",
    }
)


def _check_package_runner(
    words: list[Any],
    start: int,
    *,
    npm_opts: bool = True,
    flags: frozenset[str] | None = None,
    value_opts: frozenset[str] | None = None,
) -> int:
    """Validate runner options and return the package operand index."""
    value_opts = (
        value_opts
        if value_opts is not None
        else _RUNNER_VALUE_OPTS
        if npm_opts
        else frozenset()
    )
    flags = flags if flags is not None else _RUNNER_FLAGS if npm_opts else frozenset()
    j = start
    while j < len(words):
        tok = words[j].word
        if tok == "--":
            j += 1
            break
        if not tok.startswith("-"):
            break
        name = tok.split("=", 1)[0]
        if name in value_opts:
            nxt = j + 1 if "=" in tok else j + 2
        elif tok in flags:
            nxt = j + 1
        elif name in ("-c", "--call", "--shell-mode"):
            raise UnparseableCommand(
                f"package runner option {tok} runs a shell command string"
            )
        else:
            raise UnparseableCommand(f"package runner option {tok} cannot be validated")
        _require_literal(words, j, nxt)
        j = nxt
    if j < len(words):
        _require_literal(words, j, j + 1)
    return j


def _unwrap_npx(
    words: list[Any],
    idx: int,
    depth: int,
    acc: list[tuple[str, str, bool]],
    *,
    dynamic_args: bool,
) -> None:
    """Validate a direct package runner and expose its target command."""
    j = _check_package_runner(
        words, idx + 1, npm_opts=_basename(words[idx].word) == "npx"
    )
    if j < len(words):
        _emit(words, j, depth + 1, acc, dynamic_args=dynamic_args)


def _skip_runner_prefix(words: list[Any], k: int, tool: str) -> int | None:
    """Return the index after a where-to-run prefix at ``k``, or None."""
    w = words[k].word
    if tool == "pnpm" and w in {"recursive", "multi", "m"}:
        return _check_package_runner(words, k + 1, npm_opts=False)
    if tool == "yarn" and w == "workspace":
        _require_literal(words, k + 1, k + 2)
        return _check_package_runner(words, k + 2, npm_opts=False)
    if tool == "yarn" and w == "workspaces" and words[k + 1 : k + 2]:
        _require_literal(words, k + 1, k + 2)
        if words[k + 1].word == "foreach":
            flags, value_opts = _YARN_FOREACH_OPTS
            return _check_package_runner(
                words, k + 2, flags=flags, value_opts=value_opts
            )
    return None


def _subcommand_target(
    words: list[Any], k: int, subcommands: tuple[tuple[str, ...], ...]
) -> int | None:
    """Index of the target of a runner subcommand at ``k``, or None if none matched."""
    for sub in subcommands:
        if words[k].word == sub[0]:
            _require_literal(words, k, k + len(sub))
            got = tuple(word.word for word in words[k : k + len(sub)])
            if got == sub:
                return _check_package_runner(words, k + len(sub), npm_opts=False)
            # A global option between subcommand words hides the rest (`uv tool -q run`).
            if any(word.startswith("-") for word in got[1:]):
                raise UnparseableCommand(
                    "package runner option inside a subcommand cannot be validated"
                )
    return None


def _unwrap_subcommand_runner(
    words: list[Any],
    idx: int,
    depth: int,
    acc: list[tuple[str, str, bool]],
    *,
    dynamic_args: bool,
) -> None:
    """Expose targets of known package runner subcommands."""
    if idx + 1 >= len(words):
        return
    tool = _basename(words[idx].word)
    subcommands = _RUNNER_SUBCOMMANDS[tool]
    k = idx + 1
    if tool in _RUNNER_GLOBALS:
        k = _check_package_runner(
            words,
            k,
            flags=_RUNNER_GLOBALS[tool][0],
            value_opts=_RUNNER_GLOBALS[tool][1],
        )
    elif words[k].word.startswith("-"):
        _require_literal(words, idx + 1, len(words))
        if any(word.word == sub[0] for word in words[idx + 1 :] for sub in subcommands):
            raise UnparseableCommand(
                "package runner subcommand after global option cannot be validated"
            )
        return
    while k < len(words):
        w = words[k].word
        nxt = _skip_runner_prefix(words, k, tool)
        if nxt is not None:
            k = nxt
            continue
        if tool == "bun" and w == "exec":
            if k + 2 != len(words):
                raise UnparseableCommand("bun exec requires one shell command string")
            _require_literal(words, k + 1, k + 2)
            _extract(words[k + 1].word, depth + 1, acc, dynamic_args=dynamic_args)
            return
        _require_literal(words, k, k + 1)
        j = _subcommand_target(words, k, subcommands)
        if j is not None:
            if j < len(words):
                _emit(words, j, depth + 1, acc, dynamic_args=dynamic_args)
            return
        if tool in _RUNNER_BUILTINS and w not in _RUNNER_BUILTINS[tool]:
            _emit(words, k, depth + 1, acc, dynamic_args=dynamic_args)
        return


def _unwrap_npm(
    words: list[Any],
    idx: int,
    depth: int,
    acc: list[tuple[str, str, bool]],
    *,
    dynamic_args: bool,
) -> None:
    """``npm [global-opts] exec|x`` runs a package binary like ``npx``."""
    k = idx + 1
    while k < len(words):
        tok = words[k].word
        if tok == "--":
            k += 1
            break
        if not tok.startswith("-"):
            break
        name = tok.split("=", 1)[0]
        if name in _NPM_GLOBAL_VALUE_OPTS:
            nxt = k + 1 if "=" in tok else k + 2
        elif tok in _NPM_GLOBAL_FLAGS:
            nxt = k + 1
        else:
            raise UnparseableCommand(f"npm option {tok} cannot be validated")
        _require_literal(words, k, nxt)
        k = nxt
    if k < len(words):
        _require_literal(words, k, k + 1)
        is_exec = words[k].word in ("exec", "x")
        allowed = (
            (_NPM_GLOBAL_FLAGS - {"-y", "--yes"} if is_exec else _NPM_GLOBAL_FLAGS)
            | _NPM_GLOBAL_VALUE_OPTS
            | _NPM_SUBCOMMAND_OPTS
            | (_RUNNER_FLAGS | _RUNNER_VALUE_OPTS if is_exec else frozenset())
        )
        for m in range(idx + 1 if is_exec else k + 1, len(words)):
            tok = words[m].word
            if tok == "--":
                break
            _require_literal(words, m, m + 1)
            if tok.startswith("-") and tok.split("=", 1)[0] not in allowed:
                raise UnparseableCommand(f"npm option {tok} cannot be validated")
        if is_exec:
            j = _check_package_runner(words, k + 1)
            if j < len(words):
                _emit(words, j, depth + 1, acc, dynamic_args=dynamic_args)


def _unwrap_cargo(
    words: list[Any],
    idx: int,
    depth: int,
    acc: list[tuple[str, str, bool]],
    *,
    dynamic_args: bool,
) -> None:
    """Refuse ``cargo watch`` and inline ``--config KEY=VALUE`` overrides."""
    del depth, acc, dynamic_args
    for k in range(idx + 1, len(words)):
        tok = words[k].word
        if tok == "watch":
            raise UnparseableCommand(
                "cargo watch runs shell commands from a long-running watcher; not permitted"
            )
        if tok == "--config" or tok.startswith("--config="):
            has_eq = "=" in tok
            if not has_eq and k + 1 >= len(words):
                raise UnparseableCommand(
                    "cargo --config without value cannot be validated"
                )
            _require_literal(words, k, k + (1 if has_eq else 2))
            val = tok.split("=", 1)[1] if has_eq else words[k + 1].word
            if not re.fullmatch(r"[\w./-]+\.toml", val):
                raise UnparseableCommand(
                    f"cargo --config {val} is not a .toml file path"
                )


_UNWRAPPERS: dict[str, Callable[..., None]] = {
    **dict.fromkeys(_SHELL_INTERPRETERS, _unwrap_dash_c),
    "env": _unwrap_env,
    "xargs": _unwrap_xargs,
    "find": _unwrap_find_exec,
    **dict.fromkeys(_PREFIX_OPT_SKIPPERS, _unwrap_prefix),
    "source": _unwrap_source,
    ".": _unwrap_source,
    "npx": _unwrap_npx,
    "pnpx": _unwrap_npx,
    "bunx": _unwrap_npx,
    "uvx": _unwrap_npx,
    "npm": _unwrap_npm,
    **dict.fromkeys(_RUNNER_SUBCOMMANDS, _unwrap_subcommand_runner),
    "cargo": _unwrap_cargo,
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
