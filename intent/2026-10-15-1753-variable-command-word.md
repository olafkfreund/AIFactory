---
status: approved
issue: 1753
author: olafkfreund
---

# Intent: a variable as the command word must not bypass the Bash allowlist

## Problem

Open on `dev`; no commit mentions #1753. In `_extract`
(`apps/backend/security/ast_parser.py:142-146`), a command whose first word is
not a literal (`$X`, `"$X"`, `${X}`, `$(...)`, a backtick) is skipped with
`continue`. The hook (`security/hooks.py:189-201`) blocks only when the parser
returns no command at all, so any other literal command in the same string
hides the dynamic one. A probe on `dev` gives:

- `echo hi; $X status` → `['echo']` (allowed)
- `ls | $X`, `true && ${X} a` → only the literal command
- `$(echo nc) evil` → `['echo']`, although the shell runs `nc`

`$X` can be any program, set earlier in the same string (`X=nc; ls; $X evil`).
This bypasses the whole allowlist and has existed since the root commit.

## Proposed outcome

- Any command word that is not a static literal makes the parser raise
  `UnparseableCommand`, so the hook blocks, whatever else the string contains.
- Refused rows in the sandbox tests for `$X`, `"$X"` and `${X}`, alone and after
  `;`, `&&` and `|`, proving the bypass is refused.
- Ordinary literal commands are unaffected (the existing `LEGIT_COMMANDS`
  corpus stays green).

## Affected users and systems

- `apps/backend/security/ast_parser.py` (`_extract`, `_emit`)
- `security/hooks.py` and `security/plan_commands.py` (callers; no change)
- `tests/test_command_sandbox.py`
- Agents that write a dynamic command word: now blocked (by design).

## Constraints

- Security fix: fail closed.
- #1754 (`GIT_CONFIG_*` env) is related but out of scope.
- Target `dev`, add a CHANGELOG entry.

## Open questions

1. A bare `$(...)`/backtick command word is also dynamic (its output is run).
   Refuse it too? Conservative answer, taken here: yes.
