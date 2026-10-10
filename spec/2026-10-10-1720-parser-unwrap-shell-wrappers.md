---
status: draft
issue: 1720
intent: intent/2026-10-10-1720-parser-unwrap-shell-wrappers.md
---

# Spec: validators read the inner command after wrappers are removed

## Design

Written against #1689's `hooks.py` (branch `fix/1689-fsmonitor-git-config-audit`,
commit a605ab4b, which adds `parser.get_segments_for_validation`). PR #1722
(#1689) is still open. No code is written until it merges; then
`fix/1720-parser-unwrap-shell-wrappers` is rebased onto dev.

Root cause: validators get their input from `parser.get_command_for_validation`
(dev `hooks.py:188` and `:246`), which looks inside no wrapper and falls back
to the raw outer string. The AST walk in `security/ast_parser.py` already
unwraps `bash/sh -c`, `env`, `xargs` and `find -exec` (`:143-152`) for the
allowlist. The fix makes that same walk also produce the validator input, so
there is one unwrapping implementation and every caller goes through it.

Two source files change, plus tests and `CHANGELOG.md`. `git_validators.py`
and `parser.py` do not change.

### 1. `apps/backend/security/ast_parser.py`

- One recursive emitter replaces the per-wrapper `acc.append(...)` plus
  shell-only recursion (`:141-152`, `:170-214`):

  ```python
  def _emit(words: list[Any], idx: int, depth: int,
            acc: list[tuple[str, str, bool]], *, dynamic_args: bool = False) -> None:
      tokens = [w.word for w in words]
      acc.append((_basename(tokens[idx]), shlex.join(tokens[idx:]), dynamic_args))
      # dispatch on the name: shells -> _unwrap_dash_c, env, xargs, find,
      # and new: command / exec / timeout -> _unwrap_prefix
  ```

  `_emit` builds `tokens` itself so it has five parameters; six trips a new
  PLR0913 in the strict ruff ratchet (seen in the prototype).
- Every unwrapper calls `_emit` on its target, with `depth + 1`. So chains
  (`env timeout git`, `xargs env git`, `command exec git`, `find -exec env git`)
  are unwrapped with no extra code, and `_MAX_DEPTH` (`:55`, `:106-107`)
  bounds them.
- `dynamic_args` is passed as `True` by the xargs unwrapper and by the
  `find -exec/-execdir/-ok/-okdir` unwrapper, and is passed down unchanged by
  every unwrapper below them, including the `-c` payload. That covers
  `xargs sh -c 'git "$@"' _`, where git gets its arguments from stdin through
  the inner shell. find must set it too: its target's real arguments are the
  matched paths, not the literal `{}`. Without it this design weakens
  `find / -exec rm -rf {} +`, refused today only because the rm validator
  reads the raw outer string and sees `/`; given the segment
  `rm -rf {} +`, `validate_rm_command` returns `(True, '')` (checked here).
- `_unwrap_dash_c` (`:155-167`) accepts any short-flag group that contains
  `c` (`-c`, `-lc`, `-ec`, `-xc`), not only the exact token `-c`. Today
  `bash -lc 'git -c core.fsmonitor=x status'` and `sh -ec 'nosuchbin'` emit
  only the shell, so the inner command skips the allowlist as well as its
  validator. The non-literal payload refusal (`:160-163`) is unchanged.
- `_unwrap_env` raises `UnparseableCommand` on `-S` / `--split-string`
  (both the space and `=` forms). That flag runs its value as a command line
  and nothing parses it today (`env -S 'git -c core.fsmonitor=x status'` is
  allowed).
- New `_unwrap_prefix(words, idx, depth, acc, *, dynamic_args)` for `command`,
  `exec` and `timeout`:
  - `command`: if `-v` or `-V` is among the leading options, return (lookup
    only, runs nothing). Skip `-p`.
  - `exec`: skip `-c` and `-l`; `-a NAME` consumes its value. No operand
    (`exec 3>&1`) emits nothing.
  - `timeout`: skip `--preserve-status`, `--foreground`, `-v`; `-s/--signal`
    and `-k/--kill-after` consume their value (both forms); then skip
    DURATION.
  - Then `_emit` the target.
- New public `extract_command_segments_ast(cmd) -> list[tuple[str, str, bool]]`
  (name, segment, dynamic_args). `extract_commands_ast` keeps its signature and
  returns `[n for n, _, _ in extract_command_segments_ast(cmd)]`, so
  `plan_commands.py:177`, `check_egress` and
  `tests/test_command_sandbox.py:129-151` keep working.
- The segment starts at the command word, as written (so chmod still sees
  `tokens[0] == "chmod"`, `filesystem_validators.py:63`). Leading `VAR=`
  words are dropped, as today (`:125-131`).

### 2. `apps/backend/security/hooks.py`

- `_extract_commands_fail_closed` (`:72-90`) returns
  `(list[tuple[str, str, bool]] | None, reason)`. With bashlex it calls
  `extract_command_segments_ast`.
- Fallback branch (bashlex missing, today `:89-90`): refuse when the
  regex-extracted names include `env`, `xargs`, `exec`, `timeout` or `watch`;
  `command` without `-v`/`-V`; a shell (`bash`, `sh`, `zsh`, `dash`, `ksh`)
  with a short-flag group containing `c`; or `find` with `-exec`,
  `-execdir`, `-ok` or `-okdir`. Otherwise build
  `(name, seg, False)` from #1689's
  `get_segments_for_validation(name, split_command_segments(command)) or [command]`.
  That is the only place the regex segments are still used.
- New `_validator_refusal(cmds, root) -> str | None`, called by both
  `bash_security_hook` and `validate_command`. For each tuple whose name is
  in `VALIDATORS` (`validator_registry.py:33-54`): set the worktree root, run
  the validator on the segment, return its reason if it refuses. If it passes
  and `dynamic_args` is set, refuse with: "`{name}` run by xargs or
  find -exec takes arguments the {name} validator cannot check. Pass explicit
  arguments instead (or use `find -delete`)."
- This replaces the two duplicated validator loops (dev `:187-199` and
  `:245-256`; #1689 `:189-200` and `:246-255`), so they cannot drift apart
  again.
- No logging is added. Reasons are returned only.

### 3. Tests and CHANGELOG

- `tests/test_command_sandbox.py`: parametrized rows asserting the reason
  substring, not just `False` (listed under Verification). One unit test of
  the `extract_command_segments_ast` tuples, including `dynamic_args` through
  `xargs sh -c`. One fallback test that monkeypatches
  `security.hooks.is_available` to `False`.
- `CHANGELOG.md`: one entry under `[Unreleased]`.

### Proposed answers to the intent's open questions

Proposed defaults; the approver may change any of them.

1. **Sequencing.** Wait for PR #1722 (#1689) to merge, then rebase onto dev
   before writing code. Write this spec now against #1689's
   `get_segments_for_validation` API. Do not stack on the branch: it is still
   under review and every change there would force a rebase. The intent's
   Constraints line "local only, not pushed and no PR yet" is stale (it is
   open as PR #1722); the plan corrects it in the same step as the rebase.
2. **`xargs <validated command>`.** Refuse only when the xargs target is in
   `VALIDATORS`, after first validating its static arguments (so
   `xargs git -c core.fsmonitor=x` names `core.fsmonitor`). Other targets
   (`grep`, `wc`, `sed`) pass. Refusing every xargs would break
   `find … | xargs grep`, which the intent says must pass. Known cost:
   `find … | xargs rm` is refused. That is stricter, never weaker, and closes
   the same escape for the #364 rm path check. `find -exec <validated cmd>`
   gets the same rule, for the same reason (see Design, `dynamic_args`).
3. **Prefixes.** Unwrap `command`, `exec` and `timeout`. `timeout` is
   base-allowlisted (`project/command_registry/base.py:67`) and is a live
   bypass: `timeout 5 git -c core.fsmonitor=x status` returns `(True, '')`.
   Do not unwrap `command -v/-V`. Skip `builtin`, `nohup`, `nice`, `stdbuf`:
   not in `base.py`, so the allowlist already refuses them. Leave `time`:
   bashlex cannot parse it, so it already fails closed. `watch` (`base.py:68`)
   runs its argument through sh; that is an allowlist gap, not a validator
   gap, and goes to a follow-up issue.
4. **Without bashlex.** Refuse every wrapper and prefix (list above). The
   regex parser does not learn to unwrap, so there is one implementation.
   bashlex is a hard dependency (`apps/backend/requirements.txt:46`), so
   default installs never take this path.
5. **Nested or dynamic payloads.** Recurse, using the parser's existing
   `_MAX_DEPTH = 25` (`ast_parser.py:55`, refused deeper at `:106-107`) and
   its non-literal `-c` refusal (`:160-163`; `bash -c "$V"` is blocked). No
   new depth logic. `bash -c "bash -c 'git -c core.fsmonitor=x status'"`
   will be refused with a reason naming `core.fsmonitor`, not the accidental
   `-c bash`.

## Alternatives rejected

- **Teach `parser.get_command_for_validation` / `get_segments_for_validation`
  to unwrap.** A second unwrapping implementation (ruled out by Q4), and the
  regex parser cannot tell quoting apart.
- **Validate both the regex segments and the AST segments.** Doubles
  validator runs, and the outer `bash -c` segment keeps producing the
  accidental `-c` match, so the reason never names the real key.
- **Pass the whole command node (`tokens[0:]`) to validators.** rm and chmod
  read the first token, and the outer wrapper's `-c` would still leak in.
- **Unwrap inside each validator.** Fifteen places to get it wrong.
- **Refuse every `xargs`.** Breaks `find | xargs grep`.
- **Put the `VALIDATORS` lookup in `ast_parser`.** Makes the parser depend on
  policy. The parser only records `dynamic_args`; `hooks.py` decides.
- **Parse the `env -S` value recursively.** Refusing it is fail-closed and
  smaller; no agent run is known to need it.
- **Skip the values of `env -u` / `-C`.** Today `env -u FOO git status`
  emits `FOO` and is refused. That fails closed; fixing it is not a security
  change and goes to the follow-up.
- **Refuse a shell that reads stdin (`echo … | bash`).** A real gap, but a
  different one (no wrapper is involved); follow-up issue.
- **Refuse every non-literal argument to a validated command**
  (`git -c "$K"`). Breaks `git commit -m "$MSG"`; follow-up issue.
- **Unwrap `builtin`, `nohup`, `nice`, `stdbuf`, `time`, `watch`.** See Q3.

## Risks

- **Stricter allowlist (intended).** The inner commands of `command`, `exec`,
  `timeout` and grouped `-lc`-style shells now reach the allowlist,
  `check_egress` and `plan_commands`. `timeout 60 npm test` is refused in a
  project that does not allow npm; before, only `timeout` was checked. Agent
  runs that relied on this will see new refusals.
- **Refusal wording may change.** All names are checked against the
  allowlist before any validator runs; before, the two checks alternated per
  command. A command that fails both may show the other message first. It is
  still refused.
- **Segment drops leading `VAR=` and redirections.** No validator reads
  either. Env-based git config injection
  (`GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.fsmonitor … git status`,
  `GIT_SSH_COMMAND=… git`) is allowed today and stays allowed; it belongs to
  #1717 or a follow-up.
- **`shlex.join` re-quotes words.** bashlex `word.word` has quotes removed,
  so re-quoting is correct. `git commit -m '$(x)'` still passes.
- **Grouped-flag heuristic.** `bash script.sh -c x` is treated as a payload.
  Stricter, acceptable.
- **`find … | xargs rm`, `xargs git …` and `find … -exec <validated cmd>`
  are refused** (Q2). `find . -name '*.pyc' -exec rm {} ;` and
  `find . -exec git add {} +` pass today and will be refused; `find -delete`
  is the replacement for the first.
- **The fallback refuses `xargs grep`** (Q4). Only reachable without bashlex.
- **Rebase onto #1689.** Conflicts are certain at the two validator loops
  and in `CHANGELOG.md`. Take #1689's version, then replace both loops with
  `_validator_refusal`; keep both CHANGELOG entries.
- **No host-specific risk.** The hook runs in every agent session
  (`core/client.py:1031`) and in `tools/executor.py:286-299`; both get the
  same change.

## Verification

- **Refused, reason substring asserted** (`validate_command` and
  `bash_security_hook`):

  | Command | Reason names |
  |---|---|
  | `command git -c core.fsmonitor=x status` | `core.fsmonitor` |
  | `exec git -c core.fsmonitor=x status` | `core.fsmonitor` |
  | `bash -c 'git --upload-pack=/tmp/evil ls-remote x'` | `--upload-pack` |
  | `sh -c 'git --upload-pack=/tmp/evil ls-remote x'` | `--upload-pack` |
  | `bash -c 'git --config-env=core.fsmonitor=FOO status'` | `core.fsmonitor` |
  | `echo core.fsmonitor x \| xargs git config` | `git` run by xargs |
  | `bash -c 'git -c core.fsmonitor=x status'` | `core.fsmonitor` (not `-c bash`) |
  | `bash -c "bash -c 'git -c core.fsmonitor=x status'"` | `core.fsmonitor` |
  | `bash -lc 'git -c core.fsmonitor=x status'` | `core.fsmonitor` |
  | `timeout 5 git -c core.fsmonitor=x status` | `core.fsmonitor` |
  | `timeout -s KILL 5 git -c core.fsmonitor=x status` | `core.fsmonitor` |
  | `env timeout 5 git -c core.fsmonitor=x status` | `core.fsmonitor` |
  | `command exec git -c core.fsmonitor=x status` | `core.fsmonitor` |
  | `xargs git -c core.fsmonitor=x status` | `core.fsmonitor` |
  | `echo a \| xargs sh -c 'git "$@"' _` | `git` run by xargs |
  | `env -S 'git -c core.fsmonitor=x status'` | refused (unparseable) |
  | `command rm -rf /` | the rm reason |
  | `find . \| xargs rm` | `rm` run by xargs |
  | `find / -exec rm -rf {} +` | `rm` run by xargs or find -exec |
  | `find . -name '*.pyc' -exec rm {} ;` | `rm` run by xargs or find -exec |
  | `find /etc -exec chmod 777 {} +` | `chmod` run by xargs or find -exec |

- **Must stay refused:** `env git -c core.fsmonitor=x status`,
  `find . -exec git -c core.fsmonitor=x status {} ;`, `bash -c "$V"`.
- **Must pass:** `command -v git`, `find . -name '*.py' | xargs grep foo`,
  `bash -c 'ls && git status'`, `bash -lc 'git status'`, `exec git status`,
  `timeout 60 git status`, `env FOO=1 git status`,
  `bash -c 'npm test'` with a profile that allows npm,
  `find . -name '*.pyc' -delete`, `find . -name '*.py' -exec grep -l foo {} +`,
  `git commit -m '...$(x)'`.
- **Fallback:** with `security.hooks.is_available` patched to `False`, each
  wrapper above is refused; `git status && ls` and `command -v git` pass.
- **Unit:** exact tuples from `extract_command_segments_ast`, including
  `dynamic_args` propagation through `xargs sh -c` and set by `find -exec`.
- **Mutation checks** (by editing, since the coder cannot stash, checkout or
  restore): disable in turn the `_unwrap_prefix` dispatch, the `-c` group
  match, the `env -S` refusal, the `dynamic_args` refusal and the
  `command -v` skip; confirm the matching test fails; re-edit to restore.
- **Suites:** `pytest tests/test_command_sandbox.py tests/test_sandbox_escape_corpus.py tests/test_security.py`
  plus the plan-command tests. Prototype of the minimal core: 205 passed,
  2 skipped, no existing test changed. Run `test_security.py` with nothing
  staged (`GitCommitValidator` fails while files are staged).
- **Lint and ratchets:** `ruff format --check` and `ruff check` on
  `apps/backend apps/web-server scripts tests`;
  `python scripts/cq_ratchet.py --staged --ruff "$(command -v ruff)" --config standards/ruff.toml --paths 'apps/backend/*.py' 'apps/web-server/*.py' 'scripts/*.py'`
  and the same with `--tool mypy --mypy "$(command -v mypy)" --config standards/mypy.ini`.
  New functions fully annotated (`list[Any]` for bashlex nodes, keyword-only
  `dynamic_args`), module import for `shlex`, no aliased multi-name imports.
- **Autonomy matrix:** neither file is cited today; run
  `python scripts/gen_autonomy_matrix.py --check` after the rebase anyway.
- **CI:** `security-lint.yml`, `security-cleanup-gates.yml` and CodeQL stay
  green. No subprocess spawns are added (`test_no_unscrubbed_spawn`
  unaffected). Commit scope without `#`: `fix(security): … (#1720)`.
- **Follow-up issue** (opened with the PR): `watch` runs its argument
  through sh;
  `env -u/-C` value skipping and `env -S` parsing; shells reading stdin;
  non-literal arguments to validated commands; env-based git config
  injection (with #1717); audit of the rm, chmod and database validators
  against the wrapper corpus.
