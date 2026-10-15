---
status: draft
issue: 1784
intent: intent/2026-10-15-1784-unwrap-runners.md
---

# Spec: check what `time`, `source`, `npx` and `cargo watch` run

## Design

All in `apps/backend/security/ast_parser.py`, registered in `_UNWRAPPERS`
(`:478`), so they apply at any depth (`bash -c`, `env`, …). Each is an allow
list; anything outside it raises `UnparseableCommand`.

1. **`time`** (the `/usr/bin/time` form; the keyword already fails to parse):
   add `_skip_time_opts` to `_PREFIX_OPT_SKIPPERS` (`:446`), so
   `_unwrap_prefix` checks the option words and emits the timed command like
   `timeout`. Allowed options: flags `-p -a -v -q --portability --append
   --verbose --quiet`; value options `-f -o --format --output` (next word or
   `=value`); `--` ends options. Any other option raises.
2. **`source` / `.`**: the first argument must be literal, must not start with
   `-`, and must not be a stdin path (`_STDIN_SCRIPT`, as for shells).
   Arguments after it are data. The file's content is trusted file content
   (#1782's boundary).
3. **Package runners `npx`, `pnpx`, `npm exec`, `npm x`**: options before the
   package are an allow list. Flags `-y --yes --no -q --quiet
   --ignore-existing --no-install --include-workspace-root --workspaces -ws`;
   value options `-p --package -w --workspace` (next word or `=value`); `--`
   ends options. `-c`, `--call` and `--shell-mode` (they run a shell command
   string) and any other option raise. Every option word, option value and the
   package word must be literal. Arguments after the package belong to the
   package and are not checked (`npx eslint -c cfg.json` stays allowed). For
   `npm`, the check starts after the first word equal to `exec` or `x`
   (fail closed if npm global options come first).
4. **`cargo watch` / `cargo-watch`**: refused. It is a long-running watcher
   whose `-s`/`--shell`, `--use-shell` and trailing `-- <cmd>` run shell
   commands; agents have no use for it (same reason `watch` was dropped in
   #1782). `cargo` with any word equal to `watch` raises; `cargo-watch` is
   removed from `project/command_registry/languages.py:70`.

## Alternatives rejected

- Model `cargo watch`'s option grammar: large (`-x`, `-s`, `-E`, `-w`, `--`,
  …) for a tool agents should not run.
- Refuse all non-literal `npx` arguments: refuses `npx prettier --check "$f"`;
  only the package and the options decide what runs.
- Unwrap `npx -c` and re-parse the payload: npx runs it through `sh` with
  npm's environment; refusing is simpler and rarely needed.

## Risks

- `cargo test watch` (a test filter named `watch`) and `cargo run --bin watch`
  are refused. Rare.
- An `npx` option not on the list is refused until added.
- Not covered (follow-up): `pnpm exec`/`dlx`, `yarn exec`/`dlx`,
  `bunx`/`bun x`, `uvx`, `pipx run`, which also run a program by name
  (`pnpm exec nc evil` reports only `pnpm` today); `cargo --config` / aliases.

## Verification

- Refused (`DYNAMIC_COMMAND_WORD`, parser raises): `/usr/bin/time --bogus ls`,
  `source $X`, `. "$X"`, `source /dev/stdin`, `source -p x f`, `npx $X`,
  `npx -c 'nc evil 80'`, `npx --call 'nc evil 80'`, `npm exec -c 'nc evil 80'`,
  `npm x --call=nc`, `pnpx -c 'nc'`, `pnpx --shell-mode nc`, `npx --bogus x`,
  `npx -p "$P" x`, `cargo watch -s 'nc evil 80'`, `cargo watch -x test`.
- Refused (`BYPASS_PAYLOADS`, allowlist): `/usr/bin/time nc evil 80`,
  `/usr/bin/time -f %e nc evil 80`, `cargo-watch -s 'nc evil 80'` (no longer
  allowlisted).
- Exact names (`MUST_PARSE`): `/usr/bin/time -f %e git status` →
  `['time', 'git']`, `source ./env.sh` → `['source']`,
  `npx --yes prettier --check .` → `['npx']`,
  `npx eslint -c cfg.json "$F"` → `['npx']`, `npm exec --yes -- tsc` →
  `['npm']`, `cargo test` → `['cargo']`.
