---
status: draft
issue: 1784
author: olafkfreund
---

# Intent: allowlisted commands that run other programs must be checked

## Problem

Open on `dev`. `_UNWRAPPERS` (`apps/backend/security/ast_parser.py:478`)
covers shells, `env`, `xargs`, `find`, `command`, `exec` and `timeout`.
Other allowlisted commands also run a program or a command string, but only
their own name is checked. A probe on `dev`:

- `/usr/bin/time nc evil` → `['time']` (`time` is in
  `project/command_registry/base.py:65`; the bare `time` keyword already fails
  to parse, so it is refused).
- `source $X`, `. $X` → `['source']`, `['.']`: an unseen file is run.
- `npx -c 'nc evil'`, `npx --call …`, `npm exec -c …`, `npm x -c …`,
  `pnpx -c …` → only the runner; `-c` runs a shell command string.
  `npx $X` runs an unseen package.
- `cargo watch -s 'nc evil'`, `cargo-watch -s …`, `cargo watch -- nc evil`
  → only `cargo`; `-s`/`--shell` and the trailing command run a shell
  command. `cargo` is allowlisted for every Rust stack.

`watch` was already removed from the allowlist in #1782.

## Proposed outcome

- `time` is unwrapped like `timeout`: its command is checked.
- `source`/`.` take only a literal file (not stdin); the file's content is
  trusted, as #1782 set out.
- `npx`, `pnpx`, `npm exec`/`npm x`: options and the package must be literal;
  the shell-string options are refused.
- `cargo watch` / `cargo-watch`: words literal; `-s`/`--shell`, `--use-shell`
  and a trailing `-- <cmd>` are refused.
- Each as a fail-closed allow list (#1782's lesson), with test rows.

## Affected users and systems

- `apps/backend/security/ast_parser.py`; `tests/test_command_sandbox.py`.
- Agents using `npx -c`, `cargo watch -s`, or an unknown `npx` option: now
  refused.

## Constraints

- Fail closed; keep `MUST_PARSE` green (`npx --yes prettier --check .`).
- Out of scope (reported as a follow-up): `pnpm exec`/`dlx`, `yarn exec`/`dlx`,
  `bunx`/`bun x`, `uvx`, `pipx run`, which also run a program by name;
  `cargo --config`/aliases (config file content).

## Open questions

1. The other package runners above: include now or follow-up? Taken here:
   follow-up, to keep this PR to the issue's list.
