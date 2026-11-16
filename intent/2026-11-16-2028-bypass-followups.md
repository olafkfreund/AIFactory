---
status: draft
issue: 2028
author: olafkfreund
---

# Intent: close the follow-ups from the #1948 bypass reviews

## Problem

The script-runner allowlist (#1948, #2010) pins `make` targets, package
scripts and `./script.sh` to the base commit. Reviews found six gaps, all
inside the Job pod boundary (#1932):

1. `ConfigParser` reads base files with `git show <base>:./<file>`. Git does not
   verify a loose object's hash on read, and the agent can write into the
   worktree's git directory, so a forged object named after the base sha stands
   in for the base commit.
2. `./echo` (an untracked agent file) runs, because the command is reduced to
   its basename before the allowlist check.
3. `./build.sh`, tracked at base, is refused (basename `build.sh` is checked
   against `./build.sh` entries). Fails closed, but #1948 asked it to work.
4. Makefile detection sees only the first plain name per rule line and no
   `include`: `build.all lint: test`, `dist/app.js:` and `include common.mk`
   targets are refused. Fails closed, regression since #2010.
5. `yarn|pnpm|bun <name>` (and `bun run <name>`) are checked only if the cwd `package.json`
   defines `<name>`; a parent `package.json` the runner walks up to is missed.
6. The base sha is recorded only in `create_worktree`, so targets added upstream
   after a dev merge into a running task are refused.

## Proposed outcome

- A forged git object cannot change what the allowlist reads from the base.
- A command word containing a path is allowed only as a base-tracked `./name`.
- Base-tracked `./build.sh` works; Makefile targets of the shapes above work.
- `yarn|pnpm|bun <name>` runs only a base script or a known built-in.
- Item 6 is decided and recorded.
- Each bypass has a test that failed before the fix.

## Affected users and systems

AIFactory backend: `project/config_parser.py`, `project/structure_analyzer.py`,
`security/hooks.py`, `security/script_validators.py`, tests. Agents running in
worktrees (Job pods).

## Constraints

Fail closed on any doubt. No new dependency. No behaviour change for bare
command names (`python`, `git`). No live-cluster change.

## Open questions

None. Reviews (codex, agy) raised: Makefile `:=` assignments must not read as targets, and `git ls-tree` and `git show` resolve against different directories when cwd is a subdirectory; both go into the spec.
