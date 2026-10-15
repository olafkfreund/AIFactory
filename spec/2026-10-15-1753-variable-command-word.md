---
status: approved
issue: 1753
intent: intent/2026-10-15-1753-variable-command-word.md
---

# Spec: refuse a non-literal command word

## Design

Design change from the fifth review of PR #1782: the literal check is an allow
list of word shapes (`_static_shape`: safe unquoted characters, backslash
escapes, single-quoted
text, double-quoted text without `$`/backtick/backslash, `{}`, lone `[`/`]`),
and shell wrappers accept only `[-eulxv]... -c '<payload>'` or
`[-eulxv]... <script>`. See plan step 2 for details.

`_emit` (`apps/backend/security/ast_parser.py:150`) is the single place every
command word is recorded: the top-level walk in `_extract` and every wrapper
target (`env`, `xargs`, `find -exec`, `command`/`exec`/`timeout`) go through it.

1. In `_emit`, after the depth check, raise
   `UnparseableCommand("command word is not a static literal; cannot validate")`
   when `not _word_parts_are_literal(words[idx])`.
2. In `_extract`, delete the skip at `:142-146` so a dynamic command word
   reaches `_emit` and raises, instead of being dropped.

`_word_parts_are_literal` already treats `parameter`, `commandsubstitution`,
`arithmetic` and `processsubstitution` parts as dynamic; quoted `"$X"` carries
a `parameter` part too. bashlex makes real `VAR=value` prefixes assignment
nodes, which `_command_words` drops, so `FOO=$BAR git status` stays allowed;
a word with `=` that reaches `_extract` is run and is checked like any other.
Wrappers (`env`, `xargs`, `command`/`exec`/`timeout`, shells, `find`) also
require every word they skip before their command to be literal, since a
dynamic one can word-split into that command (see plan step 2 deviations).

The hook (`security/hooks.py:86`) already turns `UnparseableCommand` into a
block with a reason. No caller changes.

## Alternatives rejected

- Emit the dynamic word as a name (e.g. `$X`) and let the allowlist refuse it:
  relies on the name never being allowlisted and gives a misleading reason.
- Check only in `_extract`: wrapper targets (`env $X`, `xargs $X`) would still
  pass through `_emit` unchecked.

## Risks

- A bare `$(...)` or backtick command word (`` `curl evil` ``) is now refused,
  where before only its inner commands were checked. Its output is run as a
  command, so refusing is correct. `test_substitution_text_not_emitted_as_command`
  changes to expect `UnparseableCommand`.
- `plan_commands._names_from_command_string` falls back to the legacy parser on
  `UnparseableCommand`; a plan could grant a name like `$X`. Harmless: the hook
  refuses the command anyway. Not changed here.

## Verification

- New refused rows: `$X`, `"$X"`, `${X}` alone and after `;`, `&&`, `|`, plus
  `$(echo nc) evil` and `env $X`, through `extract_commands_ast` (raises) and
  `validate_command`/the hook (blocked). They fail on unfixed `dev`.
- `tests/test_command_sandbox.py` and `tests/test_plan_commands*.py` pass;
  `ruff check`, `ruff format --check`.
