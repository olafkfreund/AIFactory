---
status: draft
issue: 1807
intent: intent/2026-10-17-1807-unwrap-package-runners.md
---

# Spec: check what the remaining package runners run

## Design

All in `apps/backend/security/ast_parser.py`.

1. **`_check_package_runner(words, start, *, npm_opts=True)`** (`:863`):
   with `npm_opts=False` the flag and value-option sets are empty, so every
   option before the target raises (`-c`/`--call`/`--shell-mode` keep their
   "runs a shell command string" reason); `--` still ends options and the
   target must be literal. Default keeps `npx`/`npm exec` unchanged.
2. **Direct runners `bunx`, `uvx`, `pnpx`**: `_unwrap_npx` (`:891`) calls the
   helper with `npm_opts` true only for `npx`, then `_emit`s the target as
   now. Register `bunx` and `uvx` in `_UNWRAPPERS`.
3. **Subcommand runners**, table
   `_RUNNER_SUBCOMMANDS = {"pnpm": (("exec",), ("dlx",)), "yarn": (("exec",),
   ("dlx",)), "bun": (("x",),), "uv": (("tool", "run"),), "pipx": (("run",),)}`,
   new `_unwrap_subcommand_runner`:
   - If the word after the tool starts with `-` (a global option, which may
     take the next word as its value, so the subcommand is unknown): every
     word must be literal and none may equal a runner subcommand's first word;
     otherwise raise. Nothing is emitted (not a runner).
   - Else the word after the tool must be literal. For a subcommand whose
     first word matches, its words must be literal; on a full match the
     helper runs with `npm_opts=False` from after the subcommand and the
     target is `_emit`ted.

## Alternatives rejected

- Per-runner option allow lists (pnpm `--filter`, uvx `--from`, ...): larger,
  and each tool's option grammar differs (the `pnpx -w` bug shows the risk).
  Refusing options is fail closed; allow lists can be added on demand.
- Refusing the runners outright: breaks `pnpm exec tsc`, `uvx ruff`.

## Risks

- `pnpm -r exec ...`, `pnpm --filter x exec ...`, `uvx --from ...`,
  `pnpm dlx --package=...` are now refused (false positives, fail closed).
- `pnpm --filter web test $X`: a leading global option now requires all
  words to be literal.
- Implicit binary runs (`pnpm <bin>`, `yarn <bin>`, `uv run`, `bun run`) stay
  as before: follow-up issue.

## Verification

- `tests/test_command_sandbox.py`: refusal rows in `DYNAMIC_COMMAND_WORD`
  (parser raises), allowed rows in `MUST_PARSE` with the target named, for
  every runner. They fail on unfixed `dev`.
- `pytest tests/test_command_sandbox.py tests/test_plan_commands.py`,
  `ruff check`, `ruff format --check` on the touched files.
