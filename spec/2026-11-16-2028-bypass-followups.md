---
status: draft
issue: 2028
intent: intent/2026-11-16-2028-bypass-followups.md
---

# Spec: close the follow-ups from the #1948 bypass reviews

## Design

Each item gets a test that reproduces it first. Decisions:

**1. Verified base reads** (`project/config_parser.py`). Replace the `git show`
and `ls-tree` reads, when `ref` is set, with a walk that trusts no object it
has not hashed. `rev-parse --verify <ref>^{commit}` gives the sha; then
`git cat-file commit|tree|blob <oid>` returns raw bytes, and the walk checks
`hash(f"<type> <len>\0" + raw) == oid` (sha1 for 40 hex, sha256 for 64) for the
commit, every tree down to the file, and the blob. The commit's `tree` line
and each tree entry give the next oid. The start directory is
`rev-parse --show-prefix` so the result matches today's `./<file>` (and
`ls-tree` is now relative to the same directory as `show`). A mismatch, a
missing object or an unparsable entry returns None, which every caller already
treats as "not at base". This covers loose objects, packs and alternates alike.
`read_text` accepts a relative path (needed for includes). No gitdir
permission change: the agent needs `.git` writes for normal git use.

**2 and 3. Path-bearing command words** (`security/hooks.py`, one helper used
by `bash_security_hook` and `validate_command`). The AST step keeps returning
basenames; the helper reads the first token of each segment. No `/` in it: no
change. With a `/`:
- `./<name>` where `<name>` is in the profile's base-tracked `shell_scripts`
  (profile and `./` both resolve against the hook cwd): allowed, the command
  name becomes `./<name>`, which the profile already lists (fixes item 3).
- an absolute path inside the hook cwd: refused (agent-writable).
- any other relative path (`./echo`, `.venv/bin/pytest`, `sub/x`): refused
  (fixes item 2).
- an absolute path outside cwd (`/usr/bin/env`): unchanged.
Base-tracked scripts stay editable by the agent; that is what #1948 asked to
allow and it is inside the pod boundary. `.venv/bin/<tool>` now needs the bare
name or `python -m`; accepted as the safe side.

**4. Makefile targets** (`project/structure_analyzer.py`). Read each rule line
`targets : prerequisites`: split the target list on whitespace, accept names
without `$`, `%` or `(`, skip lines that are variable assignments
(`=`, `:=`, `::=`, `?=`, `+=`, `!=`) and names starting with `.`. Follow
`include`, `-include` and `sinclude` whose operands are plain relative names,
read from the verified base, depth 5, no revisit. An operand with `$` or a glob
adds nothing (fail closed).

**5. Shorthand runners** (`security/script_validators.py`). For yarn, pnpm and
bun, a first positional that is not in a fixed per-tool built-in list is a
script name and must be in the base scripts (drop `_is_worktree_script`).
`bun run <arg>`: `<arg>` is checked the same way unless it contains `/` or ends
in a source extension (a file, as `python x.py` already is). npm stays as is.

**6. Stale base.** Keep the behaviour: the base sha is recorded only at
`create_worktree`, and a later dev merge fails closed until the worktree is
recreated. Re-recording needs a trusted notion of "the new base" (the agent's
own commits must never qualify) that nothing in this repo supplies today.
Documented in `docs/`-level comment on `record_base_sha`.

## Alternatives rejected

- Read-only gitdir: breaks `git commit` for the agent.
- Copy base files into the baseline dir at record time: new storage and a
  second source of truth; the hash walk needs none.
- Drop the `./<name>` entries (item 3): breaks #1948's request.
- Allow `.venv/bin/*` and `node_modules/.bin/*`: both are agent-writable.
- Refresh the base after a dev merge: no trusted base source (item 6).

## Risks

- Refusing relative path commands may block agents that call `.venv/bin/x`.
  Fails closed; the message names the command.
- Built-in lists can lag the tools; an unlisted real subcommand is refused
  until added.
- Extra git subprocesses per profile build (a few `cat-file` calls).
- Fallback `HEAD` base (no recorded sha) remains agent-controlled; unchanged.

## Verification

New tests in `tests/test_base_ref_scripts.py` (and a parser test), each failing
before the fix: forged commit/tree/blob object in `.git/objects` is ignored;
`./echo` refused; `./build.sh` tracked at base allowed and untracked one
refused; Makefile shapes `build.all lint: test`, `dist/app.js:`,
`include common.mk` allowed and `X := a: b` not a target; `yarn echo` with a
parent-only agent script refused; subdirectory cwd consistent. Then the full
`tests/test_base_ref_scripts.py`, `tests/test_security*.py`,
`tests/test_command_sandbox.py` and ruff.
