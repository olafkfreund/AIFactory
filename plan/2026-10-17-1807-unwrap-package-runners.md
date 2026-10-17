---
status: approved
issue: 1807
spec: spec/2026-10-17-1807-unwrap-package-runners.md
---

# Plan: check what the remaining package runners run

Self-contained. You do not need to open the intent or the spec.

**Problem.** In `apps/backend/security/ast_parser.py`, `npx`/`pnpx`/`npm exec`
send their target through `_emit` (#1806), but `pnpm exec|dlx`,
`yarn exec|dlx`, `bunx`, `bun x`, `uvx`, `uv tool run` and `pipx run` are
checked by name only: `bunx nc evil 80` → `['bunx']`. And `pnpx` (= `pnpm
dlx`, where `-w` is a flag) uses npm's option set, where `-w` takes a value,
so `pnpx -w nc ls` is checked as `ls`.

**Approved decisions** (fail closed: anything else raises
`UnparseableCommand`).

1. Reuse `_check_package_runner` (`:863`); do not copy it. Add a keyword
   `npm_opts: bool = True`. When false, the flag and value-option sets are
   empty, so every option before the target raises; `-c`/`--call`/
   `--shell-mode` keep their reason; `--` still ends options; the target must
   be literal.
2. `_unwrap_npx` (`:891`): pass `npm_opts=_basename(words[idx].word) == "npx"`.
   Register `"bunx"` and `"uvx"` → `_unwrap_npx` in `_UNWRAPPERS` (`:983`)
   next to `"pnpx"`.
3. New `_RUNNER_SUBCOMMANDS: dict[str, tuple[tuple[str, ...], ...]] =
   {"pnpm": (("exec",), ("dlx",)), "yarn": (("exec",), ("dlx",)),
   "bun": (("x",),), "uv": (("tool", "run"),), "pipx": (("run",),)}` and
   `_unwrap_subcommand_runner(words, idx, depth, acc, *, dynamic_args)`:
   - nothing after the tool → return.
   - `words[idx + 1]` starts with `-` (global option; it may eat the next
     word, so the subcommand is unknown): `_require_literal(words, idx + 1,
     len(words))`; raise if any word after `idx` equals the first word of one
     of the tool's subcommands. Return.
   - else for each subcommand `sub`: if `words[idx + 1].word == sub[0]`:
     `_require_literal(words, idx + 1, idx + 1 + len(sub))`; if the words
     `idx + 1 .. idx + len(sub)` equal `sub`: `j = _check_package_runner(words,
     idx + 1 + len(sub), npm_opts=False)`; if `j < len(words)`:
     `_emit(words, j, depth + 1, acc, dynamic_args=dynamic_args)`; return.
     Before the loop, `_require_literal(words, idx + 1, idx + 2)` (a dynamic
     subcommand could be `dlx`).
   Register each key of `_RUNNER_SUBCOMMANDS` in `_UNWRAPPERS`.
4. Out of scope (follow-up issue): implicit binary runs `pnpm <bin>`,
   `yarn <bin>`, `uv run`, `bun run`. Accepted limits from #1806 stay.

## Steps

### Step 1 — regression tests

File: `tests/test_command_sandbox.py`. (The refusal rows go in
`DYNAMIC_COMMAND_WORD`, next to the #1784 runner rows; it is checked by the
parser, `validate_command` and the hook. `ENV_KEY_PARSER_REFUSED` is for env
keys.)

- Append to `DYNAMIC_COMMAND_WORD`, comment `# Package runners (#1807).`:
  `"pnpm exec $X"`, `"pnpm dlx -c 'nc evil 80'"`, `"pnpm $X nc evil 80"`,
  `"pnpm --filter web exec nc"`, `"yarn dlx $X"`, `"yarn exec --bogus x"`,
  `"bunx $X"`, `"bunx --bun nc"`, `"bun x -c nc"`, `"uvx $X"`,
  `"uvx --from evil nc"`, `"uv tool $X nc"`, `"uv tool run --with x nc"`,
  `"pipx run $X"`, `"pipx run --spec evil nc"`, `"pnpx -w nc ls"`.
- Append to `BYPASS_PAYLOADS`: `"bunx nc evil 80"`,
  `"pnpm exec nc evil 80"`.
- Add to `MUST_PARSE`: `"pnpm exec tsc": ["pnpm", "tsc"]`,
  `"pnpm dlx prettier --check .": ["pnpm", "prettier"]`,
  `"yarn dlx eslint .": ["yarn", "eslint"]`,
  `"yarn exec -- tsc": ["yarn", "tsc"]`,
  `"bunx prettier .": ["bunx", "prettier"]`, `"bun x tsc": ["bun", "tsc"]`,
  `"uvx ruff check .": ["uvx", "ruff"]`,
  `"uv tool run ruff check .": ["uv", "ruff"]`,
  `"pipx run black .": ["pipx", "black"]`,
  `"pnpm --filter web test": ["pnpm"]`, `"uv pip install x": ["uv"]`.

Check: `pytest tests/test_command_sandbox.py -q` — the new runner rows fail
on unfixed code (the `pnpm --filter web test` and `uv pip install x` rows
pass both before and after).

### Step 2 — the fix

File: `apps/backend/security/ast_parser.py`, decisions 1–3. Keep the
existing `npx`/`npm` behaviour (all existing rows stay green).

Check: `pytest tests/test_command_sandbox.py tests/test_plan_commands.py -q`;
`ruff check` and `ruff format --check` on both files.

Traps: no loop-variable reassignment, no magic numbers (strict ruff
ratchet); `.word` of `$X` is the text `$X`, so check literalness with
`_require_literal`, not string compares.

### Step 3 — CHANGELOG

Under `## [Unreleased]` / `### Security`, one entry after the #1784 one.

## Rollback

Revert the PR; no data or config changes.
