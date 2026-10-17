---
status: approved
issue: 1809
intent: intent/2026-10-17-1809-quoted-heredoc-time.md
---

# Spec: quoted heredocs and the `time` keyword must parse

## Design

Rewrite the string into a form bashlex accepts, parse the rewrite, and use it
only when the parse confirms the rewrite was right. All in
`apps/backend/security/ast_parser.py`, at the top of `_extract` (`:347`).

**1. `time` keyword.** A new helper prefixes a backslash to `time` where it
starts a command: at the string start or after `;`, `&`, `|`, `(`, `{`, `!`
or a newline, optionally followed by blanks, and followed by a blank, `;`,
`&`, `|`, `)` or the end. bashlex parses `\time` as the plain word `time`
(checked), and `_emit` already unwraps `time` through `_unwrap_prefix` /
`_skip_time_opts` (`:717`, #1784), so the timed command is checked and an
unknown `time` option is refused. A match inside quotes or a heredoc body
only adds a backslash to data; it cannot create a command, so no
confirmation is needed. `time` after `then`/`do`/`else` is not rewritten and
stays refused (fail closed).

**2. Quoted heredoc delimiters.** A regex finds `<<` or `<<-` (not `<<<`)
followed by a delimiter word built only from `[A-Za-z0-9_.-]`, single- or
double-quoted runs of those characters, and backslash-escaped ones, with at
least one quote or backslash. Each match is replaced by the operator plus the
delimiter with its quoting removed (the word bash compares body lines to),
and the operator's offset in the rewritten string is recorded. A delimiter
with any other character is not rewritten (bashlex keeps refusing it).

After parsing the rewrite, walk the trees: every `redirect` node whose
`type` is `<<` or `<<-` and whose `pos[0]` is a recorded offset is
confirmed, and its `heredoc` node's `value` is set to `""` (bash does no
expansion in a quoted body, so it is data; this skips the #1797 body check
for that body only). If any recorded offset is not confirmed (the `<<'X'`
was inside quotes, a comment, etc.), discard the rewrite and parse the
original string instead, which keeps today's behaviour exactly (parse or
refuse).

The rest of `_extract` (`_DEFAULT_ASSIGN` scan, `w.raw` slices) uses the
rewritten string, since node positions refer to it.

## Alternatives rejected

- **Cut the quoted body out ourselves before parsing.** Needs our own
  quote/comment tracking to know a `<<` is real; a mistake would delete real
  commands from what is checked. Letting bashlex locate the body and
  confirming the operator avoids that.
- **Catch the bashlex error and fall back to a looser parser.** Widens what
  passes for everything bashlex rejects, against fail-closed.
- **Rewrite `time` to nothing.** Loses the `time` options check and turns
  `time -x cmd` into a command named `-x`.

## Risks

- The `_DEFAULT_ASSIGN` text scan still sees a quoted body, so a literal
  `${X:=y}` with a git/LD key in a commit message body is refused. Over-
  refusal only; accepted.
- A `time` inside a quoted argument gains a backslash in what the validator
  sees (never in what runs). Validators read argument data only for git
  config keys and similar; none match `\time`. Accepted.
- Mixing a real quoted heredoc with an unconfirmed `<<'X'` in a string makes
  the whole command fall back to the original and be refused, as today.

## Verification

`tests/test_command_sandbox.py`:

- `MUST_PARSE` rows: `cat <<'EOF'` with `$(nc evil 80)` in the body →
  `["cat"]`; `cat <<"EOF"`, `<<-'EOF'`, `<<\EOF`; the commit form
  `git commit -m "$(cat <<'EOF' ... EOF\n)"` → `["git", "cat"]`;
  `time pytest -q` → `["time", "pytest"]`; `time -p git status`;
  `ls | time git status`; `echo "a <<'X'"` → `["echo"]` (fallback path).
- `BYPASS_PAYLOADS`: `time nc evil 80`, `ls | time nc evil 80`.
- `DYNAMIC_COMMAND_WORD` (parser refuses): `time cat <<EOF` with `$(nc evil
  80)` in the body; `git commit -m "$(cat <<EOF` with `$(nc evil 80)` in the
  body `)"`; `time --bogus ls`.
- The new rows fail on `dev` without the fix and pass with it.
- `pytest tests/test_command_sandbox.py tests/test_security.py`, `ruff check`,
  `ruff format --check`, `mypy` on the changed module.
