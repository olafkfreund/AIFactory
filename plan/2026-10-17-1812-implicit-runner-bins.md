---
status: approved
issue: 1812
spec: spec/2026-10-17-1812-implicit-runner-bins.md
---

# Plan: check implicit package-runner targets

Self-contained. You do not need to open the intent or the spec.

**Problem.** In `apps/backend/security/ast_parser.py`, #1813 made the
explicit runners (`pnpm exec|dlx`, `yarn exec|dlx`, `bun x`, `uv tool run`,
`pipx run`) send their target through `_emit`. These still pass as the
package manager only: `pnpm <cmd>` (falls back to running `<cmd>`),
`yarn <bin>`, `uv run <cmd>`, `bun run <bin>`, `bun <file>`,
`pnpm recursive|multi|m exec <prog>`, `yarn workspace <w> exec <prog>`,
`yarn workspaces foreach ... exec <prog>`, `bun exec '<shell string>'`.
Today `extract_commands_ast("pnpm nc evil 80") == ["pnpm"]`.

**Approved decisions** (fail closed: anything else raises
`UnparseableCommand`).

1. Reuse `_check_package_runner` (`:927-958`); do not copy it. Add keywords
   `flags: frozenset[str] | None = None` and `value_opts: frozenset[str] |
   None = None`. When not None they replace the sets chosen by `npm_opts`.
   Nothing else in it changes.
2. `_RUNNER_SUBCOMMANDS` (`:852-858`): `"bun": (("x",), ("run",))`,
   `"uv": (("tool", "run"), ("run",))`.
3. New tables next to it:
   - `_RUNNER_GLOBALS: dict[str, tuple[frozenset[str], frozenset[str]]]`
     (flags, value options):
     `pnpm`: flags `-r --recursive -w --workspace-root -s --silent --parallel
     --stream`, values `--filter -F -C --dir`;
     `yarn`: flags `-s --silent`, values `--cwd`;
     `bun`: flags `--bun --silent`, values `--cwd`.
   - `_YARN_FOREACH_OPTS` (flags, values): flags `-A --all -R --recursive -W
     --worktree -p --parallel -i --interlaced -t --topological
     --topological-dev -v --verbose`, values `--include --exclude --from -j
     --jobs`.
   - `_RUNNER_BUILTINS: dict[str, frozenset[str]]` (built-in subcommands
     that do not run a named program; scripts via `run` stay unchecked like
     `npm run`):
     `pnpm`: add approve-builds audit bin c cache cat-file cat-index config
     create dedupe deploy doctor env fetch find-hash help i ignored-builds
     import init install install-test it la licenses link list ll ln ls
     outdated pack patch patch-commit patch-remove prune publish rb rebuild
     remove rm root run run-script self-update server setup start store t
     test tst un uninstall unlink up update upgrade why;
     `yarn`: add audit autoclean bin cache check config constraints create
     dedupe explain generate-lock-entry global help import info init install
     licenses link list login logout npm outdated owner pack patch
     patch-commit plugin policies publish rebuild remove run search set stage
     tag team test unlink unplug up upgrade upgrade-interactive version
     versions why workspaces;
     `bun`: a add audit build c completions create help i info init install
     link outdated patch patch-commit pm publish remove repl rm test unlink
     update upgrade why.
     (`yarn node` is deliberately absent, so `node` is checked as a command.)
4. `_unwrap_subcommand_runner` (`:974-1009`):
   - `uv`/`pipx` (no `_RUNNER_BUILTINS` entry): flow unchanged.
   - `pnpm`/`yarn`/`bun`: `k = _check_package_runner(words, idx + 1,
     flags=..., value_opts=...)` with `_RUNNER_GLOBALS[tool]` (this replaces
     the "leading global option" branch for these tools). Then loop while
     `k < len(words)`, `w = words[k].word`:
     - `pnpm` and `w in {"recursive", "multi", "m"}`:
       `k = _check_package_runner(words, k + 1, npm_opts=False)`; continue.
     - `yarn` and `w == "workspace"`: `k = _check_package_runner(words,
       k + 2, npm_opts=False)` after `_require_literal(words, k + 1, k + 2)`;
       continue (if `k + 1 >= len(words)` return).
     - `yarn`, `w == "workspaces"` and next word is `foreach`:
       `_require_literal(words, k + 1, k + 2)`; `k = _check_package_runner(
       words, k + 2, flags=..., value_opts=...)` with `_YARN_FOREACH_OPTS`;
       continue.
     - `bun` and `w == "exec"`: require `k + 2 == len(words)` and
       `_require_literal(words, k + 1, k + 2)`; then `_extract(words[k +
       1].word, depth + 1, acc, dynamic_args=dynamic_args)`; return.
     - the #1813 subcommand loop, with `k` in place of `idx + 1`.
     - no subcommand matched and `w not in _RUNNER_BUILTINS[tool]`:
       `_emit(words, k, depth + 1, acc, dynamic_args=dynamic_args)`.
     - return.
5. Out of scope: `pnpm|yarn run` scripts and yarn's `run <bin>` fallback
   (like `npm run`; scripts are agent-writable anyway), `create`/`init`
   (accepted like `npm create`), and the accepted limits of #1806/#1813.

## Steps

### Step 1 — regression tests

File: `tests/test_command_sandbox.py`.

- In `DYNAMIC_COMMAND_WORD` (`:98`), remove `"pnpm --filter web exec nc"`
  (`:243`; it now resolves to `nc` instead of raising) and append, comment
  `# Implicit package runners (#1812).`:
  `"pnpm --bogus nc"`, `"pnpm recursive --bogus exec nc"`,
  `"yarn workspace $W nc"`, `"yarn workspaces foreach --bogus exec nc"`,
  `"bun exec $X"`, `"bun exec 'nc evil 80' x"`, `"bun exec"`,
  `"bun --preload x.ts run tsc"`, `"uv run --with evil nc"`,
  `"bun run --watch nc"`.
- Append to `MUST_PARSE` (`:284`), same comment:
  `"pnpm --filter web exec nc": ["pnpm", "nc"]`,
  `"pnpm nc evil 80": ["pnpm", "nc"]`, `"pnpm build": ["pnpm", "build"]`,
  `"pnpm install": ["pnpm"]`, `"pnpm run build": ["pnpm"]`,
  `"pnpm -r nc": ["pnpm", "nc"]`,
  `"pnpm recursive exec nc": ["pnpm", "nc"]`,
  `"pnpm multi exec nc": ["pnpm", "nc"]`, `"pnpm m nc": ["pnpm", "nc"]`,
  `"yarn nc evil 80": ["yarn", "nc"]`, `"yarn install": ["yarn"]`,
  `"yarn": ["yarn"]`, `"yarn workspace web exec nc": ["yarn", "nc"]`,
  `"yarn workspace web nc": ["yarn", "nc"]`,
  `"yarn workspace web add x": ["yarn"]`,
  `"yarn workspaces foreach -A exec nc": ["yarn", "nc"]`,
  `"yarn workspaces foreach --include web -p nc": ["yarn", "nc"]`,
  `"yarn workspaces list": ["yarn"]`,
  `"uv run pytest -q": ["uv", "pytest"]`, `"uv sync": ["uv"]`,
  `"bun run nc": ["bun", "nc"]`, `"bun nc": ["bun", "nc"]`,
  `"bun test": ["bun"]`, `"bun install": ["bun"]`,
  `"bun exec 'nc evil 80'": ["bun", "nc"]`,
  `"bun exec 'git status && ls'": ["bun", "git", "ls"]`.

Check: `pytest tests/test_command_sandbox.py -q` — the new rows fail on the
unfixed parser (the `install`/`run build`/`list`/`sync`/`test` rows pass
already as false-positive guards).
Traps: keep existing rows; `MUST_PARSE` is a dict (no duplicate keys).

### Step 2 — parser

File: `apps/backend/security/ast_parser.py`, decisions 1-4.

Check: `cd apps/backend && pytest ../../tests/test_command_sandbox.py -q`
(or from the repo root `pytest tests/test_command_sandbox.py -q`) all green;
`ruff check` and `ruff format --check` on both files.
Traps: `_extract` is defined at `:395`, above, so no import change; keep
`_check_package_runner`'s default behaviour (npx/npm exec rows must stay
green); `bun exec` must not accept `-c`-style options; a prefix with nothing
after it (`pnpm recursive`, `yarn workspace web`) just returns.

## Rollback

Revert the commits; the parser returns to #1813 behaviour.
