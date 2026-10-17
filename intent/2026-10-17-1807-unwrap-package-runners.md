---
status: approved
issue: 1807
author: olafkfreund
---

# Intent: the remaining package runners must have their target checked

## Problem

Open on `dev`. #1806 (#1784) made `npx`, `pnpx` and `npm exec`/`npm x`
check the program they run: `_check_package_runner` and `_unwrap_npx` in
`apps/backend/security/ast_parser.py:863-902` send the package operand
through `_emit`, so it meets the allowlist. These runners still pass as a
plain allowlisted name (`_UNWRAPPERS`, `:983`):

- `pnpm exec <cmd>`, `pnpm dlx <pkg>` (`pnpm` allowlisted for Node stacks,
  `project/command_registry/package_managers.py:16`)
- `yarn dlx <pkg>`, `yarn exec <cmd>`
- `bunx <pkg>`, `bun x <pkg>`
- `uvx <pkg>`, `uv tool run <pkg>` (`languages.py:34-35`)
- `pipx run <pkg>` (`languages.py:20`)

So `bunx nc evil 80` or `pnpm dlx $X` passes unchecked.

Also: `pnpx` reuses the npm option set, where `-w` takes a value. `pnpx` is
`pnpm dlx`, whose `-w` is a flag, so `pnpx -w nc ls` is checked as `ls`
while pnpm runs `nc ls`.

## Proposed outcome

- Each runner's target goes through `_emit`, reusing `_check_package_runner`,
  so the allowlist and `bash -c` parsing apply to it.
- Their options before the target are refused (no option set is checked
  for them yet); a dynamic target or subcommand is refused.
- `pnpx` uses the same strict rule.

## Affected users and systems

- `apps/backend/security/ast_parser.py`; `tests/test_command_sandbox.py`.
- Agents using e.g. `uvx --from x y`, `pnpm --filter web exec tsc`: now
  refused; the plain forms (`pnpm exec tsc`, `uvx ruff check .`) keep working
  when the target is allowlisted.

## Constraints

- Fail closed; small; reuse the #1806 helper, do not duplicate it.
- Accepted limits (not fixed here): `npm config set`/`pkg set`/`explore`/
  `init`/`create`, `NODE_OPTIONS`/`npm_config_*` prefixes, stdin symlinks
  made earlier. Real containment is OS isolation (#1757, #1732).

## Open questions

1. Runners that run a local binary implicitly (`pnpm <bin>`, `yarn <bin>`,
   `uv run <cmd>`, `bun run <bin>`): out of the issue's list. Taken here:
   follow-up issue.
