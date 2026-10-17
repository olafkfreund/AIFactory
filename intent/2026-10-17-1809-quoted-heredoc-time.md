---
status: draft
issue: 1809
author: olafkfreund
---

# Intent: quoted heredocs and the `time` keyword must parse

## Problem

The agent Bash validator (`apps/backend/security/ast_parser.py`, `_extract`
at `:347`) parses every command with bashlex and refuses anything bashlex
cannot parse. bashlex fails on two ordinary forms (checked on `dev`):

- **Quoted heredoc delimiters.** `cat <<'EOF'`, `cat <<"EOF"`, `<<\EOF`,
  `<<E'O'F`, `<<-'EOF'` raise `ParsingError: here-document ... delimited by
  end-of-file (wanted "'EOF'")`: bashlex compares body lines to the delimiter
  with its quotes still on. This blocks the common commit form
  `git commit -m "$(cat <<'EOF' ... EOF)"`.
- **The `time` keyword.** `time pytest -q` raises `NotImplementedError: type =
  {time command}`, and `a | time b` raises `ParsingError`. `/usr/bin/time` is
  already unwrapped (#1784) because bashlex sees it as a plain word.

Both fail closed, so this is a usability bug, not a security hole.

## Proposed outcome

- A command with a quoted heredoc parses; its body is treated as data (bash
  does no expansion in it), so `$(...)` text in a quoted body is not refused.
- `time [opts] <cmd>` parses and `<cmd>` is checked exactly as for
  `/usr/bin/time`; `time nc evil 80` is still blocked.
- Unquoted heredocs keep today's checks (#1797): `$(...)` in their body is
  refused, including inside `git commit -m "$(cat <<EOF ...)"`.
- Every other bashlex parse failure is still refused.

## Affected users and systems

Agent sessions whose Bash tool calls go through `security.validate_command`
and the bash security hook. No server, host or UI change.

## Constraints

- Fail closed: a rewrite the parser cannot confirm must not widen what is
  allowed.
- Smallest change, in `ast_parser.py` only; tests in
  `tests/test_command_sandbox.py`.
- Accepted limits named by the lead are out of scope (npm config/pkg/explore/
  init/create, `NODE_OPTIONS`, `npm_config_*`, earlier `/dev/stdin` links).

## Open questions

None.
