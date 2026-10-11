---
status: approved
issue: 1720
spec: spec/2026-10-10-1720-parser-unwrap-shell-wrappers.md
---

# Plan: validators read the inner command after wrappers are removed

Worktree `/mnt/code/Source-home/GitHub/AIFactory-1720`, branch
`fix/1720-parser-unwrap-shell-wrappers`, base `origin/dev`, spec approved at
`6d5e300a`. All paths are repo-relative. Python with bashlex and pytest:
`PY=/mnt/code/Source-home/GitHub/AIFactory/apps/backend/.venv/bin/python`
(the worktree has no venv). Put that venv's `bin` on `PATH` before committing
so the pre-commit hook runs.

## Approved decisions (self-contained summary)

**Problem in one line.** The allowlist sees the inner command of `bash -c`,
`env`, `xargs` and `find -exec` (the bashlex walk in
`apps/backend/security/ast_parser.py`), but the validators (git, rm, chmod, db)
get their input from the regex parser, which looks inside no wrapper. So
`bash -c 'git -c core.fsmonitor=x status'`, `command rm -rf /`,
`timeout 5 git -c core.fsmonitor=x status` and `env -S '...'` reach a validator
as the outer string, or not at all. The fix makes the AST walk the only
unwrapper, for both the allowlist names and the validator input.

0. **State (D0).** #1689 has merged: `origin/dev` contains `ed266886`
   "fix(security): ... (#1689) (#1722)". On 2026-10-11 this branch is 7 ahead
   (docs only) and 5 behind `origin/dev` (`4470e4c7`, #1728, touches no file
   cited here), so it is still on pre-#1689 code.
   Step 1 rebases before any code edit. **Every line number below is a
   post-rebase (`origin/dev`) line.** `ast_parser.py` is identical on both;
   `hooks.py`, `parser.py`, `git_validators.py`, `tests/test_command_sandbox.py`
   and `CHANGELOG.md` differ.
1. **Sequencing (D1).** Wait for #1722, then rebase; do not stack. The
   precondition is met. The intent's stale Constraints line
   (`intent/...:85-87`, "local only, not pushed and no PR yet") is corrected in
   the rebase step.
2. **One implementation (D2).** The bashlex walk in `ast_parser.py` is the only
   place unwrapping happens. `parser.py` and `git_validators.py` do not change.
   Source files that change: `ast_parser.py`, `hooks.py`. Plus
   `tests/test_command_sandbox.py` and `CHANGELOG.md`.
3. **Emitter (D3).** One recursive
   `_emit(words: list[Any], idx: int, depth: int, acc: list[tuple[str, str, bool]], *, dynamic_args: bool = False) -> None`.
   It builds the tokens itself (five parameters; a sixth trips PLR0913), appends
   `(_basename(tokens[idx]), shlex.join(tokens[idx:]), dynamic_args)`, and
   dispatches by name: shells → `_unwrap_dash_c`; `env`, `xargs`, `find` → their
   unwrappers; `command`, `exec`, `timeout` → `_unwrap_prefix`.
4. **Recursion (D4).** Every unwrapper calls `_emit` on its target with
   `depth + 1`, so chains (`env timeout git`, `xargs env git`,
   `command exec git`, `find -exec env git`) need no extra code. The bound is
   the existing `_MAX_DEPTH = 25` (`ast_parser.py:53-55`). The existing refusal
   (`:106-107`) **moves** from `_extract` to the top of `_emit`; it is not
   duplicated and no other depth logic is added. Left in `_extract` it bounds
   only `-c` nesting: `env env … ls` recurses through `_emit` alone and was
   measured unbounded (30 and 400 `env`s both accepted; Python's recursion
   limit would be the only stop, as an uncaught `RecursionError`).
5. **`dynamic_args` (D5).** `xargs` and `find -exec/-execdir/-ok/-okdir` set it
   to `True`. Every unwrapper below passes it on unchanged, including into a
   `-c` payload (covers `xargs sh -c 'git "$@"' _`). `find` must set it because
   `rm -rf {} +` passes `validate_rm_command`.
6. **Grouped `-c` (D6).** `_unwrap_dash_c` accepts any short-flag group
   containing `c` (`-c`, `-lc`, `-ec`, `-xc`): `tok.startswith("-")` and not
   `tok.startswith("--")` and `"c" in tok[1:]`. The non-literal payload refusal
   is kept. `bash script.sh -c x` is treated as a payload (stricter, accepted).
7. **`env -S` (D7).** `_unwrap_env` raises `UnparseableCommand` on
   `--split-string`, `--split-string=...`, and any short-flag group containing
   `S` (`-S`, `-S'...'` attached, `-iS`). Decision from test design: match
   `tok.startswith("-S")` too; `env` has no other option starting with `-S`.
   On dev today `env -S 'git -c core.fsmonitor=x status'` returns `['env']` and
   is allowed. `env -u/-C` value skipping is unchanged (follow-up).
8. **Prefixes (D8).** New `_unwrap_prefix` for `command`, `exec`, `timeout`:
   - `command`: any `-v`/`-V` among the leading options → return without
     emitting; `-p` is skipped.
   - `exec`: `-c`, `-l` skipped; `-a NAME` consumes its value; no operand
     (`exec 3>&1`) → emit nothing.
   - `timeout`: `--preserve-status`, `--foreground`, `-v` skipped;
     `-s`/`--signal` and `-k`/`--kill-after` consume their value (space form,
     `=` form, attached `-sKILL`); then DURATION is skipped.
   - Then `_emit(words, j, depth + 1, acc, dynamic_args=dynamic_args)`.
   - Not unwrapped: `builtin`, `nohup`, `nice`, `stdbuf` (not allowlisted in
     `apps/backend/project/command_registry/base.py`), `time` (bashlex fails
     closed on it). `watch` goes to the follow-up.
9. **Public API (D9).** New
   `extract_command_segments_ast(command_string) -> list[tuple[str, str, bool]]`
   returning `(name, segment, dynamic_args)`. `extract_commands_ast` keeps its
   signature and returns `[n for n, _, _ in extract_command_segments_ast(...)]`,
   so `plan_commands.py:177`, `check_egress` and existing unit tests work
   unchanged.
10. **Segment shape (D10).** A segment starts at the command word as written,
    so `chmod` still sees `tokens[0] == "chmod"`
    (`filesystem_validators.py:58-64`). Leading `VAR=` words and redirections
    are dropped. bashlex `word.word` is unquoted, so `shlex.join` re-quoting is
    correct.
11. **Hooks extraction (D11).** `hooks._extract_commands_fail_closed` returns
    `(list[tuple[str, str, bool]] | None, reason)` and calls
    `extract_command_segments_ast` when bashlex is available.
12. **No-bashlex fallback (D12).** Refuse when the regex names include `env`,
    `xargs`, `exec`, `timeout` or `watch`; when `command` appears without
    `-v`/`-V`; when a shell (`bash/sh/zsh/dash/ksh`) has a short-flag group
    containing `c`; or when `find` has `-exec/-execdir/-ok/-okdir`. Refusal
    text (constant label plus the name, never logged):
    ``Command blocked: shell wrapper `{name}` cannot be validated without bashlex.``
    Otherwise build `(name, seg, False)` from
    `get_segments_for_validation(name, split_command_segments(command)) or [command]`.
    This is the only place regex segments are still used; the regex parser does
    not learn to unwrap. Refusing `xargs grep` in the fallback is accepted.
13. **Shared validator pass (D13).** New
    `_validator_refusal(cmds, root) -> str | None` in `hooks.py`, used by both
    `bash_security_hook` and `validate_command`. For each tuple whose name is in
    `VALIDATORS` it sets the worktree root, runs the validator on the segment,
    resets the root, and returns the reason if refused. If the validator passes
    and `dynamic_args` is set it returns:
    ``"`{name}` run by xargs or find -exec takes arguments the {name} validator cannot check. Pass explicit arguments instead (or use `find -delete`)."``
    Static arguments are validated first, so `xargs git -c core.fsmonitor=x`
    names `core.fsmonitor`.
14. **Ordering (D14).** Every name is checked against the allowlist first, then
    `_validator_refusal` runs. The wording for a command that fails both may
    change; tests assert substrings. `check_egress` still gets the names list.
15. **xargs/find targets (D15).** A target in `VALIDATORS` under xargs or
    `find -exec` is refused; other targets (`grep`, `wc`, `sed`) pass. Known
    stricter cost: `find . | xargs rm`, `find . -name '*.pyc' -exec rm {} \;`,
    `find . -exec git add {} +` become refused; `find -delete` replaces the
    first two.
16. **No logging, no spawns (D16).** Reasons are returned only.
17. **Tests (D17).** See ## Tests. Two corrections to the spec's Verification
    table, recorded here: `find /etc -exec chmod 777 {} +` is refused by the
    chmod validator first (`got: 777`, `777` not in `SAFE_CHMOD_MODES`,
    `filesystem_validators.py:15-28`), so the dynamic_args path is tested by an
    added row `find . -exec chmod 755 {} +`; and the find terminator is written
    `\\;` in Python, matching the existing `BYPASS_PAYLOADS`.
18. **CHANGELOG (D18).** One entry under `[Unreleased]` / `### Security`
    (`CHANGELOG.md:1-3`), keeping #1689's entry. Commit
    `fix(security): ... (#1720)` with no `#` in the scope.
19. **Gates and follow-up (D19).** ruff format/check, `cq_ratchet` ruff and
    mypy (full annotations, `list[Any]` for bashlex nodes, keyword-only
    `dynamic_args`, `import shlex` as a module import, no aliased multi-name
    imports), the pytest suites, `gen_autonomy_matrix --check`,
    `security-lint.yml`, `security-cleanup-gates.yml`, CodeQL. Open the
    follow-up issue with the PR (contents in step 5).

**Spec citation drift (not a blocker; the approved spec is not edited).** The
spec's hooks citations `_extract_commands_fail_closed :72-90` and dev loops
`:187-199/:245-256` are pre-rebase lines; post-rebase they are `:76-94` and
`:189-200/:246-255`. `VALIDATORS` is `validator_registry.py:31-55` (entries
33-54), not 33-54. `TestAstExtractorUnit` is `tests/test_command_sandbox.py:185-213`
(calls at 189-211), not 129-151. All other spec citations were verified exact.

**Ruff limits that shape the code** (`standards/ruff.toml`): max-args 5,
max-branches 12, max-complexity 10. Hence a dispatch dict in `_emit`, three
per-name option skippers under `_unwrap_prefix`, and a separate
`_fallback_wrapper_refusal` in `hooks.py`. Behaviour is as in D8 and D12.
Two more strict rules the prototype hit: `os.path.basename` in new
`hooks.py` code is PTH119 (+1 regresses the per-rule ratchet; use
`Path(word).name`), and `_unwrap_xargs`/`_unwrap_find_exec` ignore the
incoming `dynamic_args` (they always pass `True`), so they start with
`del dynamic_args` to stay clear of ARG001.

**Prototype (2026-10-11).** Steps 2-4 were applied in a scratch worktree of
`origin/dev` exactly as written here: 320 passed, 2 skipped; default ruff
format/check clean; both ratchets 0 regressed (`ast_parser.py` improved);
`gen_autonomy_matrix --check` ok; M1-M12 each fail exactly the tests listed.

**Handoff.** Five steps edit files across five files, so steps 2-5 go to the
`coder` agent once this plan is approved: start it with this path and step 2,
then send each later step with `SendMessage` to the same agent. Step 1 is done
by the session (the coder cannot rebase). Review with a fresh `opus` agent
given only this path and `git diff`.

## Steps

1. **Rebase and fix the stale intent line (D0, D1).** Session, not coder.
   - `git fetch origin && git rebase origin/dev`. Commits ahead are docs-only;
     no code conflict expected.
   - `intent/2026-10-10-1720-parser-unwrap-shell-wrappers.md:85-87`: replace
     "local only, not pushed and no PR yet" (and the "not on dev yet / Until it
     lands" sentence) with "#1689 merged to dev as PR #1722 (`ed266886`)".
     Commit `docs(intent): note #1689 merged as PR #1722 (#1720)`.

   → verify by `git merge-base --is-ancestor origin/dev HEAD && echo ok`, then
   baseline `$PY -m pytest -q tests/test_command_sandbox.py tests/test_sandbox_escape_corpus.py tests/test_security.py tests/test_plan_commands.py`
   = 205 passed, 2 skipped (nothing staged).
   Traps: CHANGELOG conflict → keep both entries; autonomy matrix conflict →
   regenerate with `scripts/gen_autonomy_matrix.py`, never hand-merge. Commit
   scope must not contain `#`.

2. **`tests/test_command_sandbox.py`: tests first (D17).**
   - Imports (`:19-31`): add `extract_command_segments_ast` to the existing
     `security.ast_parser` import (not aliased); add `from security import hooks`,
     `from pathlib import Path`, `from typing import Any`.
   - Helper reusing the `asyncio.run` pattern at `:151-163`:
     `_hook(command, cwd) -> dict[str, Any]` calling
     `hooks.bash_security_hook({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)})`.
   - Add `WRAPPER_REFUSED` (32 rows), `TestWrapperUnwrapRefused`,
     `WRAPPER_ALLOWED` (13 rows), `TestWrapperUnwrapAllowed`,
     `test_bash_c_npm_allowed_in_node_project`, `TestWrapperFallbackWithoutBashlex`
     and the `TestAstExtractorUnit` additions, exactly as listed in ## Tests.
   - Rows at `:189-211` stay unchanged.

   → verify by `$PY -m pytest -q tests/test_command_sandbox.py`: **one
   collection error** (`ImportError: cannot import name
   'extract_command_segments_ast'`), no tests run. That is the expected red;
   old classes cannot run until step 3. `ruff format --check tests` and
   `ruff check tests` clean (run `ruff format tests/test_command_sandbox.py`
   first; the prototype's rows needed reflowing).
   Traps: `tests/` is outside the ratchet but under default `ruff format --check`
   and `ruff check tests`. Do not commit step 2 alone (the import error would
   break CI); steps 2-5 land in one commit.

3. **`apps/backend/security/ast_parser.py`: AST walk is the only unwrapper (D2-D10).**
   - `:17-19` module docstring: mention `command`/`exec`/`timeout` and grouped `-c`.
   - `:30-32`: add `import shlex` and `from typing import Any`.
   - `_extract` (`:105-152`): signature
     `_extract(command_string, depth, acc: list[tuple[str, str, bool]], *, dynamic_args: bool = False)`.
     Keep the depth refusal (`:105-107`), the `VAR=` skip (`:124-133`) and the
     non-literal command-word skip (`:139-140`). Replace `:135` and `:141-152`
     (tokens, `acc.append(name)`, if/elif dispatch) with
     `_emit(words, idx, depth, acc, dynamic_args=dynamic_args)`.
   - Move the depth refusal `:106-107` out of `_extract` to the top of `_emit`
     (D4).
   - New `_emit` after `:152` (D3), dispatching through a module dict
     `_UNWRAPPERS: dict[str, Callable[..., None]]` (shells → `_unwrap_dash_c`,
     `env`, `xargs`, `find`, and `command`/`exec`/`timeout` → `_unwrap_prefix`).
   - `_unwrap_dash_c` (`:155-167`): signature `(words, idx, depth, acc, *, dynamic_args)`,
     tokens built locally; `:159` → grouped-flag match (D6); keep `:160-164`;
     `:165` → `_extract(payload, depth + 1, acc, dynamic_args=dynamic_args)`.
   - `_unwrap_env` (`:170-183`): before the flag skip at `:175`, raise
     `UnparseableCommand` per D7; replace `:179-182` with
     `_emit(words, j, depth + 1, acc, dynamic_args=dynamic_args)`.
   - `_unwrap_xargs` (`:186-203`): `del dynamic_args` first; replace `:199-201` with
     `_emit(words, j, depth + 1, acc, dynamic_args=True)`; keep the value skip
     at `:194` and the comment at `:203`.
   - `_unwrap_find_exec` (`:206-214`): `del dynamic_args` first; replace `:211-214` with
     `_emit(words, j + 1, depth + 1, acc, dynamic_args=True)`. The segment
     includes the trailing `{} ;/+`; acceptable because D13 refuses validated
     targets.
   - New `_unwrap_prefix(words, idx, depth, acc, *, dynamic_args)` after
     `:214`, looking up `_skip_command_opts`, `_skip_exec_opts`,
     `_skip_timeout_opts` (each `(tokens: list[str], j: int) -> int | None`,
     `None` = do not emit) per D8.
   - `:217-231`: new `extract_command_segments_ast` takes the body of
     `:225-231` (bashlex check, empty → `[]`, `_extract`);
     `extract_commands_ast` becomes the D9 one-liner, signature and docstring kept.

   → verify by `$PY -m pytest -q tests/test_command_sandbox.py::TestAstExtractorUnit tests/test_plan_commands.py`
   green; `ruff format --check apps/backend && ruff check apps/backend`;
   `git add apps/backend/security/ast_parser.py`, both `cq_ratchet` commands
   from ## Tests, then `git reset -q -- apps/backend/security/ast_parser.py`
   (index only; the working tree is untouched). Not `git add -N`: on a tracked
   file it stages nothing, `git diff --cached` is empty and the ratchet checks
   0 files and passes vacuously (measured).
   Traps: max-args 5 (keyword-only `dynamic_args` counts); annotate bashlex
   nodes `list[Any]`; no new depth logic, every unwrapper passes `depth + 1`;
   `env` now emits non-shell targets too, so `plan_commands.py:177` sees more
   inner names (stricter, intended; that is why plan-command tests run here).

4. **`apps/backend/security/hooks.py`: one validator path (D11-D16).**
   - `:21`: import `extract_command_segments_ast` (instead of
     `extract_commands_ast`) and keep `is_available` imported by name, so
     `monkeypatch.setattr(hooks, "is_available", ...)` works.
   - `:24-28`: keep `extract_commands`, `get_segments_for_validation`,
     `split_command_segments` (fallback only).
   - `_extract_commands_fail_closed` (`:76-94`): return type
     `tuple[list[tuple[str, str, bool]] | None, str]`; bashlex branch calls
     `extract_command_segments_ast`; replace the fallback
     `return extract_commands(command), ""` (`:93-94`) with: `segments =
     split_command_segments(command)`, `names = extract_commands(command)`, a
     call to the new `_fallback_wrapper_refusal(segments, names)` → `(None,
     reason)` if it returns a name, else the D12 tuple list
     `[(n, seg, False) for n in names for seg in get_segments_for_validation(n, segments) or [command]]`.
     The helper (prototype, ratchet-clean):

     ```python
     _FALLBACK_WRAPPERS = {"env", "xargs", "exec", "timeout", "watch"}
     _FALLBACK_SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
     _FALLBACK_FIND_ACTIONS = {"-exec", "-execdir", "-ok", "-okdir"}

     def _fallback_wrapper_refusal(segments: list[str], names: list[str]) -> str | None:
         for name in names:
             if name in _FALLBACK_WRAPPERS:
                 return name
         for seg in segments:
             words = seg.split()
             if not words:
                 continue
             name = Path(words[0]).name  # not os.path.basename: PTH119
             flags = words[1:]
             if name == "command" and not any(f in ("-v", "-V") for f in flags):
                 return name
             if name in _FALLBACK_SHELLS and any(
                 f.startswith("-") and not f.startswith("--") and "c" in f[1:] for f in flags
             ):
                 return name
             if name == "find" and _FALLBACK_FIND_ACTIONS.intersection(flags):
                 return name
         return None
     ```
   - New `_validator_refusal(cmds: list[tuple[str, str, bool]], root: str) -> str | None`
     after `:94` (D13): `token = set_worktree_root(root)`; `try` validator(seg);
     `finally reset_worktree_root(token)`.
   - `bash_security_hook` (`:97-211`; edits at `:161-207`): unpack tuples into `names`; allowlist
     loop over names only (`:179-187`); delete `segments = split_command_segments(command)`
     (`:173`), the dead `allowed = profile.get_all_allowed_commands()` (`:176`),
     and the validator block (`:189-200`); add
     `if (r := _validator_refusal(cmds, cwd)): return {"decision": "block", "reason": r}`;
     `check_egress(command, names)` (`:207`; signature at `egress.py:92`).
   - `validate_command` (`:214-257`; edits at `:232-257`): same change; delete `:239` and
     `:246-255`; `if (r := _validator_refusal(cmds, str(project_dir))): return False, r`.

   → verify by `$PY -m pytest -q tests/test_command_sandbox.py tests/test_sandbox_escape_corpus.py tests/test_security.py tests/test_plan_commands.py`
   = 320 passed, 2 skipped, nothing staged; ruff and both ratchets on
   `ast_parser.py` and `hooks.py` (stage with `git add`, run, `git reset -q --
   <files>`; not `git add -N`); `python scripts/gen_autonomy_matrix.py --check`
   (if it cites `hooks.py` lines, regenerate, do not hand-edit); then the
   mutation checks M1-M12 in ## Tests.
   Traps: D14 wording changes → assert substrings; D16 no logging and no
   spawns (CodeQL `py/clear-text-logging-sensitive-data`, `py/log-injection`;
   `test_no_unscrubbed_spawn`); `test_security.py`'s `GitCommitValidator` check
   fails while anything is staged.

5. **`CHANGELOG.md`, commit, follow-up, PR (D18, D19).**
   - `CHANGELOG.md:1-3`: add a #1720 line under `[Unreleased]` / `### Security`,
     above the #1689 entry, keeping it.
   - Commit steps 2-5 as
     `fix(security): unwrap command/exec/timeout and grouped -c in the AST walk (#1720)`.
   - Follow-up issue, opened with the PR: `watch`; `env -u/-C/-S` parsing;
     stdin shells (`sh < x`, `bash -s`); non-literal arguments to validated
     commands; env-based git config injection (`GIT_CONFIG_*`, link #1717); an
     audit of the rm, chmod and db validators; the stricter cost of the
     `xargs rm` and `find -exec git add` refusals.
   - PR to `dev`: body links intent, spec, plan and the follow-up issue, and
     says which steps the coder did.

   → verify by CI green: default ruff, `security-lint.yml`,
   `security-cleanup-gates.yml`, CodeQL, `gen_autonomy_matrix --check`.
   Traps: CHANGELOG conflict at merge → keep both entries; scope without `#`.

### Shared files with in-flight issues

- **#1719 (git-dir write guard)** appends a hook to `security/hooks.py`, a
  helper to `filesystem_validators.py`, and `TestGitDirWriteGuard` to
  `tests/test_command_sandbox.py`. Merge **#1720 first** (it rewrites existing
  bodies; #1719 only appends). #1719 then re-cites `hooks.py` lines and checks
  it uses names-only `check_egress` and the tuple return of
  `_extract_commands_fail_closed`, if it calls it.
- **#1717** no file overlap; linked through the follow-up item.
- **#1708, #1624, #1627, #1620** cite only `tests/test_security.py`, which
  #1720 runs but does not edit. No ordering constraint.
- **#1631, #1632, #1692, #1633** (merging now): CHANGELOG conflicts only.

## Tests

All in `tests/test_command_sandbox.py`. Every refused row asserts a reason
substring, not only `False`; otherwise the timeout, `exec -a` and `env -S`
mutations would be hidden by the allowlist refusing a misparsed token such as
`5` or `x`.

**`WRAPPER_REFUSED` (32 rows), class `TestWrapperUnwrapRefused`** with the
`temp_dir` fixture: `test_validate_command_refuses` asserts
`ok is False and reason in r`; `test_hook_refuses` asserts
`out["decision"] == "block" and reason in out["reason"]`.

| Command | Substring |
|---|---|
| `command git -c core.fsmonitor=x status` | `core.fsmonitor` |
| `exec git -c core.fsmonitor=x status` | `core.fsmonitor` |
| `bash -c 'git --upload-pack=/tmp/evil ls-remote x'` | `--upload-pack` |
| `sh -c 'git --upload-pack=/tmp/evil ls-remote x'` | `--upload-pack` |
| `bash -c 'git --config-env=core.fsmonitor=FOO status'` | `core.fsmonitor` |
| `echo core.fsmonitor x \| xargs git config` | `` `git` run by xargs `` |
| `bash -c 'git -c core.fsmonitor=x status'` | `core.fsmonitor` |
| `bash -c "bash -c 'git -c core.fsmonitor=x status'"` | `core.fsmonitor` |
| `bash -lc 'git -c core.fsmonitor=x status'` | `core.fsmonitor` |
| `timeout 5 git -c core.fsmonitor=x status` | `core.fsmonitor` |
| `timeout -s KILL 5 git -c core.fsmonitor=x status` | `core.fsmonitor` |
| `env timeout 5 git -c core.fsmonitor=x status` | `core.fsmonitor` |
| `command exec git -c core.fsmonitor=x status` | `core.fsmonitor` |
| `xargs git -c core.fsmonitor=x status` | `core.fsmonitor` |
| `echo a \| xargs sh -c 'git "$@"' _` | `` `git` run by xargs `` |
| `env -S 'git -c core.fsmonitor=x status'` | `could not be safely parsed` |
| `command rm -rf /` | `rm target '/'` |
| `find . \| xargs rm` | `` `rm` run by xargs `` |
| `find / -exec rm -rf {} +` | `` `rm` run by xargs or find -exec `` |
| `find . -name '*.pyc' -exec rm {} \\;` | `` `rm` run by xargs or find -exec `` |
| `find /etc -exec chmod 777 {} +` | `got: 777` |
| `env git -c core.fsmonitor=x status` (must stay refused) | `core.fsmonitor` |
| `find . -exec git -c core.fsmonitor=x status {} \\;` (must stay refused) | `core.fsmonitor` |
| `bash -c "$V"` (must stay refused) | `could not be safely parsed` |
| `find . -exec chmod 755 {} +` | `` `chmod` run by xargs or find -exec `` |
| `env --split-string='git -c core.fsmonitor=x status'` | `could not be safely parsed` |
| `env -S'git -c core.fsmonitor=x status'` | `could not be safely parsed` |
| `timeout -sKILL 5 git -c core.fsmonitor=x status` | `core.fsmonitor` |
| `timeout --signal=KILL -k 1 5 git -c core.fsmonitor=x status` | `core.fsmonitor` |
| `exec -a x git -c core.fsmonitor=x status` | `core.fsmonitor` |
| `sh -ec 'git --upload-pack=/tmp/evil ls-remote x'` | `--upload-pack` |
| `timeout 60 npm test` | `npm` (intended stricter allowlist) |

(`\\;` is the Python source spelling; `\|` is table escaping for `|`.)

**`WRAPPER_ALLOWED` (13 rows), class `TestWrapperUnwrapAllowed`**:
`test_validate_command_allows` asserts `(True, "")`, `test_hook_allows` asserts
`out == {}`. Rows: `command -v git`; `find . -name '*.py' | xargs grep foo`;
`bash -c 'ls && git status'`; `bash -lc 'git status'`; `exec git status`;
`timeout 60 git status`; `env FOO=1 git status`; `find . -name '*.pyc' -delete`;
`find . -name '*.py' -exec grep -l foo {} +`; `git commit -m '...$(x)'`;
`command -v nc`; `timeout -s KILL 5 git status`; `exec 3>&1`.
Plus `test_bash_c_npm_allowed_in_node_project(node_project)` running
`bash -c 'npm test'` through both paths (fixture at `tests/conftest.py:412`).

**`TestWrapperFallbackWithoutBashlex` (11 tests)**: each does
`monkeypatch.setattr(hooks, "is_available", lambda: False)` then
`validate_command`. Refused, asserting `without bashlex`: `env git status`,
`echo a | xargs grep foo`, `exec git status`, `timeout 5 git status`,
`watch ls`, `command git status`, `bash -lc 'ls'`, `bash -c 'ls'`,
`find . -exec ls {} +`. Allowed: `git status && ls`, `command -v git`.

**`TestAstExtractorUnit` additions (13 tests)**, exact output checked against
bashlex and `shlex.join`:

- `test_segment_tuples` (6 rows) on `extract_command_segments_ast`:
  - `echo a | xargs sh -c 'git "$@"' _` →
    `[("echo","echo a",False), ("xargs","xargs sh -c 'git \"$@\"' _",False), ("sh","sh -c 'git \"$@\"' _",True), ("git","git '$@'",True)]`
  - `find . -exec env git -c core.fsmonitor=x status {} +` →
    `[("find","find . -exec env git -c core.fsmonitor=x status '{}' +",False), ("env","env git -c core.fsmonitor=x status '{}' +",True), ("git","git -c core.fsmonitor=x status '{}' +",True)]`
  - `timeout -s KILL 5 git -c core.fsmonitor=x status` →
    `[("timeout","timeout -s KILL 5 git -c core.fsmonitor=x status",False), ("git","git -c core.fsmonitor=x status",False)]`
  - `command -v nc` → `[("command","command -v nc",False)]`
  - `exec 3>&1` → `[("exec","exec",False)]`
  - `exec -a x git status` → `[("exec","exec -a x git status",False), ("git","git status",False)]`
- `test_env_split_string_raises` (4 rows, each raises `UnparseableCommand`):
  `env -S 'git status'`, `env -S'git status'`, `env --split-string='git status'`,
  `env --split-string 'git status'`.
- `test_wrapper_chain_depth_bounded`: `extract_command_segments_ast("env " * 30 + "ls")`
  raises `UnparseableCommand` (D4; pins the depth refusal in `_emit`).
- `test_grouped_dash_c_unwrapped`: `extract_commands_ast("bash -lc 'nc x'") == ["bash", "nc"]`.
- `test_long_option_containing_c_not_treated_as_dash_c`:
  `extract_commands_ast("bash --norc scripts/deploy.sh") == ["bash"]`.

**Commands** (nothing staged while pytest runs):

```
$PY -m pytest -q tests/test_command_sandbox.py tests/test_sandbox_escape_corpus.py tests/test_security.py tests/test_plan_commands.py
ruff format --check apps/backend apps/web-server scripts tests
ruff check apps/backend apps/web-server scripts tests
# after `git add <files>` (ratchets read --staged; `git add -N` stages nothing
# for a tracked file), then `git reset -q -- <files>` before pytest again:
python scripts/cq_ratchet.py --staged --ruff "$(command -v ruff)" --config standards/ruff.toml --paths 'apps/backend/*.py' 'apps/web-server/*.py' 'scripts/*.py'
python scripts/cq_ratchet.py --staged --tool mypy --mypy "$(command -v mypy)" --config standards/mypy.ini --paths 'apps/backend/*.py' 'apps/web-server/*.py' 'scripts/*.py'
python scripts/gen_autonomy_matrix.py --check
```

Expected counts:

| File | Baseline (dev) | After |
|---|---|---|
| `test_command_sandbox.py` | 72 | 187 (+64 refused, +26 allowed, +1 npm, +11 fallback, +13 unit) |
| `test_sandbox_escape_corpus.py` | 15 passed, 2 skipped (bwrap) | unchanged |
| `test_security.py` | 107 | unchanged |
| `test_plan_commands.py` | 11 | unchanged |
| Total | 205 passed, 2 skipped | 320 passed, 2 skipped |

**Mutation checks.** Apply each by editing, run
`$PY -m pytest -q tests/test_command_sandbox.py -k "<names>"`, confirm the
named tests fail, restore by re-editing (the coder has no stash/checkout/restore),
confirm green. After all, the full file is 187 passed again.

| # | Mutation | Must fail |
|---|---|---|
| M1 | Delete the `command`/`exec`/`timeout` → `_unwrap_prefix` entries | `command rm -rf /`, `timeout 5 git -c…`, `command git -c…` (both paths) |
| M2 | `-c` match back to `tok == "-c"` | `bash -lc 'git -c…'`, `sh -ec …`, `test_grouped_dash_c_unwrapped` |
| M2b | Drop the `not tok.startswith("--")` guard | `test_long_option_containing_c_not_treated_as_dash_c` |
| M3 | Delete the `env -S`/`--split-string` raise | the three `env -S`/`--split-string` rows, `test_env_split_string_raises` |
| M4 | Delete the `dynamic_args` return in `_validator_refusal` | `find / -exec rm -rf {} +`, `find . -exec chmod 755 {} +`, `find . \| xargs rm`, `xargs sh -c 'git "$@"'` |
| M5 | `_unwrap_dash_c` passes `dynamic_args=False` to the payload | `test_segment_tuples[xargs sh]`, the `xargs sh -c 'git "$@"'` rows |
| M6 | find's `_emit` passes `dynamic_args=dynamic_args` not `True` (and drop its `del dynamic_args`) | `find / -exec rm -rf {} +`, `find . -exec chmod 755 {} +`, `test_segment_tuples[find]` |
| M7 | Delete the `command -v/-V` return | allowed `command -v nc`, `test_segment_tuples[command -v]` |
| M8 | `timeout -s` does not consume its value | allowed `timeout -s KILL 5 git status`, refused `timeout -s KILL 5 git -c…` (substring) |
| M9 | `exec -a` does not consume NAME | `exec -a x git -c…` (substring), `test_segment_tuples[exec -a]` |
| M10 | Delete the `_validator_refusal` call in `validate_command` only (then in the hook only) | the `test_validate_command_refuses` validator rows fail while `test_hook_refuses` stays green (and vice versa) |
| M11 | Delete the fallback wrapper refusal | all 9 fallback refused rows |
| M12 | Delete the depth refusal at the top of `_emit` | `test_wrapper_chain_depth_bounded` |

## Rollback

Code, tests and CHANGELOG land in one `fix(security)` commit, so rollback is
`git revert <merge-sha>` on dev through a PR. No state or data migration; the
parser and hooks return to dev behaviour. Keep the `intent/`, `spec/` and
`plan/` commits. Re-run `python scripts/gen_autonomy_matrix.py --check` after
the revert. If only the stricter allowlist hurts agent runs (for example
`timeout 60 npm test` refused), add the inner tool to the project profile
instead of reverting. Reverting reopens the `env -S`, `timeout`, `command` and
grouped `-c` bypasses.

## Approver decisions (2026-10-11)

- Steps 2-4 land in one commit (no xfail split).

## Deviations (recorded during implementation)

- The coder did steps 2-4 and the step-5 CHANGELOG; the session model did
  step 1, the commit, follow-up and PR.
- `test_segment_tuples` is parametrized over its 6 rows (a loop gave 315, not
  320). `_unwrap_find_exec`/`_unwrap_xargs` carry `del dynamic_args  # why`.
  `_skip_timeout_opts` returns `j + 1`; `_unwrap_prefix` does the bounds check.
- Measured: 320 passed / 2 skipped; M1-M12 each fail; ratchets 1 improved,
  0 regressed.
- Opus review (CHANGES), fixed by a coder (verified and ratchet-cleaned by the
  session model): four validator bypasses where a wrapper option's value was
  emitted as the command:
  1. env abbreviated `--split-string` (`--s`, `--spl`): any `--s` prefix and
     any unknown long option now raise.
  2. exec grouped `-a` (`-la`, `-cla`) consumes NAME; `-aNAME` is attached.
  3. xargs: `-i`/`-e`/`-l` are attached-only; a value letter after the first
     position (`-tI`) or an unknown long option without `=` raises.
  4. env `-u`/`-C`/`-a` and `--unset`/`--chdir`/`--argv0` (unique prefix)
     consume their value. D7 had deferred this; pulled in because it is a full
     bypass of the git validator.
  16 refused and 5 allowed rows added (362 passed / 2 skipped); each fix has a
  mutation that fails its rows. Deferred to the follow-up: `watch`, the
  interpreter bypasses (`awk system()`, `source`, `| sh`) and the no-bashlex
  fallback (approver decision pending), plus the LOW false refusals
  (`timeout --sig`, `bash -c -e`).
- Opus re-review (CHANGES), fixed by the session model: env, xargs and exec
  kept reading options after the point where the real tool stops, so a
  dash-named word the tool would run as the command was skipped. Now env stops
  at `--`, a lone `-` or the first `VAR=val`; xargs takes the word after `--`
  (and a lone `-`) as the command; exec treats a lone `-` as the command. Six
  refused rows (`env FOO=1 -C ls`, `env - -C ls`, `env -- -u ls`,
  `xargs -- -C ls`, `exec - ls`, ...) and three allowed rows; real GNU env
  confirms `env FOO=1 -- x` runs `--`. One mutation per fix fails its rows
  (8, 2, 2). 380 passed / 2 skipped. LOW false refusals (`xargs -0n1`,
  `--max-args 1`, abbreviated env flags) fail closed and are left.
