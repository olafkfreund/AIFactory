---
status: draft
issue: 1812
intent: intent/2026-10-17-1812-implicit-runner-bins.md
---

# Spec: check implicit package-runner targets

## Design

All in `apps/backend/security/ast_parser.py`.

1. **Reuse `_check_package_runner`** (`:927`). Add keyword overrides
   `flags: frozenset[str] | None = None, value_opts: frozenset[str] | None =
   None`; when given they replace the sets `npm_opts` picks. Everything else
   (refusing `-c`/`--call`/`--shell-mode`, `--` ending options, literal
   checks, unknown option raises) is unchanged. Used below to skip the
   global options of `pnpm`/`yarn`/`bun` and the options of
   `yarn workspaces foreach`.
2. **Tables.**
   - `_RUNNER_SUBCOMMANDS`: add `("run",)` to `bun` and to `uv`.
   - `_RUNNER_GLOBALS: dict[str, tuple[frozenset, frozenset]]` (flags, value
     options) for `pnpm` (`-r --recursive -w --workspace-root -s --silent
     --parallel --stream` / `--filter -F -C --dir`), `yarn` (`-s --silent` /
     `--cwd`), `bun` (`--bun --silent` / `--cwd`). Any other option before
     the subcommand raises. `bun` runtime options such as `--preload` stay
     refused.
   - `_RUNNER_BUILTINS: dict[str, frozenset[str]]`: the built-in subcommands
     of `pnpm`, `yarn`, `bun` that do not run a named program (install, add,
     run for pnpm/yarn, test, ...). Plan lists them.
3. **`_unwrap_subcommand_runner`** (`:974`). For `uv`/`pipx` (no builtins
   entry) the current flow stays. For `pnpm`/`yarn`/`bun`:
   - `k = _check_package_runner(words, idx + 1, flags=..., value_opts=...)`
     with the tool's globals; return if nothing follows.
   - Prefixes that select where to run, then continue with the next word:
     `pnpm recursive|multi|m`, `yarn workspace <w>`, `yarn workspaces
     foreach [foreach options]`. The word after a prefix is found again with
     `_check_package_runner` (no options for the first two; the foreach
     flag/value sets for the last).
   - `bun exec`: exactly one more word, literal; parse it with `_extract`
     (as `bash -c` does) at `depth + 1`. Anything else raises.
   - Subcommand match (`exec`, `dlx`, `x`, `run`): unchanged from #1813.
   - Else, a word not in the tool's builtins is `_emit`ted as the command:
     `pnpm nc` → `nc`; `pnpm build` → `build` (refused unless allowlisted).
4. Tests in `tests/test_command_sandbox.py`: refusals in
   `DYNAMIC_COMMAND_WORD`/`BYPASS_PAYLOADS`, allowed forms in `MUST_PARSE`.
   `"pnpm --filter web exec nc"` moves from `DYNAMIC_COMMAND_WORD` to
   `BYPASS_PAYLOADS`: the parser now resolves it to `nc` (refused by the
   allowlist) instead of raising.

## Alternatives rejected

- Rely on OS isolation only (#1757, #1732): the issue asks for the parser
  to check what it can; containment is still the backstop.
- Allow unknown first words (treat as scripts): leaves `pnpm nc evil 80`
  open.
- Read `package.json` to tell scripts from bins: the file is
  agent-writable, and it adds I/O to a pure parser.
- A second options loop: duplicates `_check_package_runner`.

## Risks

- Script shorthands (`pnpm build`, `yarn lint`, `bun run dev`) and options
  on `uv run`/`bun run` are refused. Workaround: `pnpm run <script>`,
  `yarn run <script>`, `uv run <cmd>` without options.
- A built-in missing from the list is refused (fail closed), not opened.
- `pnpm run`/`yarn run`/`npm run` stay unchecked script runners; yarn
  berry's `yarn run <bin>` fallback is accepted like `npm run` (scripts are
  agent-writable anyway). `pnpm create`/`yarn create`/`bun create` match
  the accepted `npm create` limit.
- `bun exec` runs Bun Shell, parsed here with bash grammar; only literal
  strings bashlex accepts pass.

## Verification

`pytest tests/test_command_sandbox.py -q` (new rows fail on unfixed code),
`ruff check` / `ruff format --check` on both files, mypy as configured.
