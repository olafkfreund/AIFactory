---
status: approved
issue: 1809
spec: spec/2026-10-17-1809-quoted-heredoc-time.md
---

# Plan: quoted heredocs and the `time` keyword must parse

Self-contained. You do not need to open the intent or the spec.

**Problem.** The Bash validator (`apps/backend/security/ast_parser.py`,
`_extract` at `:347`) refuses anything bashlex cannot parse. bashlex fails on
quoted heredoc delimiters (`<<'EOF'`, `<<"EOF"`, `<<\EOF`, `<<-'EOF'`: it
compares body lines to the delimiter with its quotes on) and on the `time`
keyword (`NotImplementedError`). That blocks
`git commit -m "$(cat <<'EOF' ... EOF)"` and `time pytest -q`.

**Approved decisions.**

1. Before parsing, prefix `\` to a `time` that starts a command (string
   start, or after `;`, `&`, `|`, `(`, `{`, `!`, newline, then optional
   blanks; followed by blank, `;`, `&`, `|`, `)` or end). bashlex parses
   `\time` as the word `time`; `_emit` already unwraps `time` via
   `_unwrap_prefix`/`_skip_time_opts` (`:717`, #1784), so the timed command is
   checked. No confirmation needed: inside quotes or a body it only changes
   data the validator reads, never what runs.
2. Then rewrite each quoted heredoc delimiter (`<<` or `<<-`, not `<<<`;
   delimiter built only from `[A-Za-z0-9_.-]`, `'…'`/`"…"` runs of those, or
   `\x` escapes, with at least one quote/backslash; followed by whitespace,
   `;&|<>()` or end) to its unquoted form, recording the offset of the
   delimiter word in the rewritten string.
3. Parse the rewrite. Walk the trees: a `redirect` node of type `<<`/`<<-`
   whose `output.pos[0]` is a recorded offset is confirmed, and its
   `heredoc.value` is set to `""` (a quoted body has no expansion: data, so
   the #1797 body check is skipped for that body only). If parsing the
   rewrite fails or any offset is unconfirmed, discard the heredoc rewrite
   and parse the `time`-only rewrite, which behaves as today (parse or
   refuse).
4. The rest of `_extract` uses the text that was parsed (node positions
   refer to it). The `_DEFAULT_ASSIGN` scan still sees quoted bodies:
   accepted over-refusal.
5. Anything else bashlex rejects stays refused. `time` after
   `then`/`do`/`else` stays refused.

## Steps

### Step 1 — regression tests

File: `tests/test_command_sandbox.py`. Use plain Python string literals with
`\n` for newlines, like the existing heredoc rows (`:238`, `:336`).

- `BYPASS_PAYLOADS` (list at `:47`, append before the closing `]` at `:77`),
  with a comment `# The time keyword (#1809).`:
  `"time nc evil 80"`, `"ls | time nc evil 80"`,
  `"cat <<'EOF'\nx\nEOF\nnc evil 80"` (a command after a quoted body is
  still checked).
- `DYNAMIC_COMMAND_WORD` (list from `:92`, append before its closing `]`),
  with a comment `# A command inside time or an unquoted heredoc body (#1809).`:
  `"time cat <<EOF\n$(nc evil 80)\nEOF"`,
  `'git commit -m "$(cat <<EOF\n$(nc evil 80)\nEOF\n)"'`,
  `"time --bogus ls"`.
- `MUST_PARSE` (dict from `:255`, append before its closing `}`), with a
  comment `# Quoted heredoc bodies are data; time runs its command (#1809).`:
  - `"cat <<'EOF'\n$(nc evil 80)\nEOF": ["cat"]`
  - `'cat <<"EOF"\nhi\nEOF': ["cat"]`
  - `"cat <<-'EOF'\n\thi\n\tEOF": ["cat"]`
  - `"cat <<\\EOF\nhi\nEOF": ["cat"]`
  - `'git commit -m "$(cat <<\'EOF\'\nfix: x\n\nbody text\nEOF\n)"': ["git", "cat"]`
  - `"time pytest -q": ["time", "pytest"]`
  - `"time -p git status": ["time", "git"]`
  - `"ls | time git status": ["ls", "time", "git"]`
  - `"echo \"a <<'X'\"": ["echo"]`

Check: `apps/backend/.venv/bin/pytest tests/test_command_sandbox.py -q -k
"must_parse or bypass or Dynamic"` — on unfixed code the new `MUST_PARSE` rows
fail (expected), except `echo "a <<'X'"`, which passes already.
Traps: do not reformat other rows; keep each new row on one line where it
fits 88 columns (ruff format will check).

### Step 2 — parser change

File: `apps/backend/security/ast_parser.py`.

a. After `_DEFAULT_ASSIGN` (`:114`) add:

```python
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
        return bashlex.parse(text)
    except (bashlex.errors.ParsingError, NotImplementedError, Exception) as exc:
        # Fail closed: anything we cannot parse is treated as a block.
        raise UnparseableCommand(f"could not parse command: {exc}") from exc
```

b. In `_extract` (`:347`), replace the `try: trees = bashlex.parse(...)
except ...: raise UnparseableCommand(...)` block with:

```python
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
```

The rest of `_extract` is unchanged (it keeps reading `command_string`).

Check: `apps/backend/.venv/bin/pytest tests/test_command_sandbox.py -q`
all pass; `ruff check` and `ruff format --check` on both files; `mypy
apps/backend/security/ast_parser.py` if configured.
Traps: a bashlex tree may share nodes, but only through the same kinds
`_collect_command_nodes` walks; recursion depth is bounded by the parse.
Keep the existing fail-closed comment. Do not touch `_collect_command_nodes`.

### Step 3 — CHANGELOG

File: `CHANGELOG.md`, `## [Unreleased]` → `### Security`, add one bullet
after the #1797 bullet: quoted heredocs and `time` parse (#1809), body of a
quoted heredoc is data, `time` checks its command, unquoted bodies keep the
#1797 check.

**Deviation (step 1).** The commit-form row's body says `body text`, not
`body $(y)`: bashlex scans the raw text of a `"$(…)"` word and surfaces a
`$(…)` found in a heredoc body nested there as a command, so `y` would be
extracted too (over-checking, fail-closed; pre-existing). Added the
"command after a quoted body" bypass row.

**Deviation (step 2).** `_parse` assigns `trees: list[Any] = bashlex.parse(text)`
and returns it after the `try`, so the mypy `--strict` ratchet does not count a
new `no-any-return`.

## Tests

```
apps/backend/.venv/bin/pytest tests/test_command_sandbox.py tests/test_security.py -q
apps/backend/.venv/bin/ruff check apps/backend/security/ast_parser.py tests/test_command_sandbox.py
apps/backend/.venv/bin/ruff format --check apps/backend/security/ast_parser.py tests/test_command_sandbox.py
```

All pass. New rows fail on `dev` without step 2.

## Rollback

Revert the PR commits; the validator returns to refusing both forms.
