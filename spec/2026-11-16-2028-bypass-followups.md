---
status: approved
issue: 2028
intent: intent/2026-11-16-2028-bypass-followups.md
---

# Spec: close the follow-ups from the #1948 bypass reviews

## Design

Each item gets a test that reproduces it first. Decisions:

**1. Verified base reads** (`project/config_parser.py`). Replace the `git show`
and `ls-tree` reads, when `ref` is set, with a walk that trusts no object it
has not hashed. When `ref` is a 40 or 64 hex sha it is the commit oid itself
(no `rev-parse`, so a tag or branch named like the sha cannot stand in); for
`HEAD` (no recorded base) it is resolved once. `git cat-file commit|tree|blob
<oid>` returns raw bytes, and the walk checks `hash(f"<type> <len>\0" + raw) ==
oid` (sha1 for 40 hex, sha256 for 64) for the commit, every tree down to the
file, and the blob. The commit's `tree` line and each tree entry give the next
oid. The start directory is `rev-parse --show-prefix`, so `read_text`,
`file_exists` and `glob_files` all resolve against the same directory (today
`show ./x` follows cwd but `ls-tree` follows it too only by accident).
A mismatch, a missing object or an unparsable entry returns None, which every
caller already treats as "not at base". This covers loose objects, packs and
alternates alike. `read_text` accepts a relative path (needed for includes;
`..` and absolute paths are refused). No gitdir permission change: the agent
needs `.git` writes for normal git use.

**2. Path-bearing command words** (`security/hooks.py`, one helper used by
`bash_security_hook` and `validate_command`). The AST step keeps returning
basenames; the helper reads the first token of each segment (wrapper targets
such as `env ./x` and `xargs` are their own segments). No `/` in it: no
change. With a `/`, the command is refused unless it is an absolute path whose
normalised directory is a system directory: `/bin`, `/sbin`, `/usr/bin`,
`/usr/sbin`, `/usr/local/bin`, `/run/current-system/sw/bin`, or
`/nix/store/<x>/bin`. Everything else (`./echo`, `sub/x`, `.venv/bin/pytest`,
`/repo/echo`, `/tmp/echo`) is agent-writable and refused, whatever `cd`, `env -C`
or `-execdir` does to the directory. Agents use the bare name or
`python -m`.

**3. `./build.sh` tracked at base: decision, drop.** The profile's `./<name>`
entries can never match (the hook checks basenames), and making them match
means trusting a path against a directory the command may change (`cd sub &&
./build.sh`, `env -C sub`, `find -execdir`) and a file the agent can replace
or symlink. Safest: keep it refused, with the message from item 2 saying to run
`bash build.sh`, which the validator already treats as trusted script content.
The dead `./<name>` entries in `script_commands` stay (profile format); a test
pins the refusal so nobody half-enables them.

**4. Makefile targets** (`project/structure_analyzer.py`). Read each rule line
(not tab-indented recipe lines, comments stripped)
`targets : prerequisites` with `:`/`::` not followed by `:` or `=`: split the target list on whitespace, accept names
without `$`, `%` or `(`, skip lines that are variable assignments
(`=`, `:=`, `::=`, `?=`, `+=`, `!=`) and names starting with `.`. Follow
`include`, `-include` and `sinclude` whose operands are plain relative names,
read from the verified base, depth 5, no revisit. An operand with `$` or a glob
adds nothing (fail closed).

**5. Shorthand runners** (`security/script_validators.py`). For yarn, pnpm
and bun, a first positional that is not in `_RUNNER_BUILTINS[tool]` (the list
`ast_parser` already keeps) is a script name and must be in the base scripts;
drop `_is_worktree_script`. `bun run <arg>`: `<arg>` must be a base script
(`bun run x.ts` is already refused as a command name by the allowlist). npm is
unchanged. `--` handling in `_extract_pkg_positionals` is not touched.

**6. Stale base.** Keep the behaviour: the base sha is recorded only at
`create_worktree`, and a later dev merge fails closed until the worktree is
recreated. Re-recording needs a trusted notion of "the new base" (the agent's
own commits must never qualify) that nothing in this repo supplies today.
Documented in `docs/`-level comment on `record_base_sha`.

## Alternatives rejected

- Read-only gitdir: breaks `git commit` for the agent.
- Copy base files into the baseline dir at record time: new storage and a
  second source of truth; the hash walk needs none.
- Allow `./<name>` for base scripts (item 3): path-vs-cwd trust, see item 3.
- Allow `.venv/bin/*`, `node_modules/.bin/*` or absolute paths outside cwd: all agent-writable.
- Refresh the base after a dev merge: no trusted base source (item 6).

## Risks

- Refusing relative path commands may block agents that call `.venv/bin/x`.
  Fails closed; the message names the command.
- Built-in lists can lag the tools; an unlisted real subcommand is refused
  until added.
- Extra git subprocesses per profile build (a few `cat-file` calls).
- A nested repo (`git init sub` plus copied objects) still reads the recorded root's base files: the prefix is taken from the nested repo. Pre-existing, not in #2028; noted for a follow-up.
- Fallback `HEAD` base (no recorded sha) remains agent-controlled; unchanged.

## Verification

New tests in `tests/test_base_ref_scripts.py` (and a parser test), each failing
before the fix: forged commit/tree/blob object in `.git/objects` is ignored;
`./echo`, `cd sub && ./echo`, `/tmp/echo` and `./build.sh` refused while
`/usr/bin/env` and bare names still pass; a tag named like the base sha is ignored; Makefile shapes `build.all lint: test`, `dist/app.js:`,
`include common.mk` allowed and `X := a: b` not a target; `yarn echo` with a
parent-only agent script refused; subdirectory cwd consistent. Then the full
`tests/test_base_ref_scripts.py`, `tests/test_security*.py`,
`tests/test_command_sandbox.py` and ruff.
