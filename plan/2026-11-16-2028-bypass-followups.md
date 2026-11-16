---
status: approved
issue: 2028
spec: spec/2026-11-16-2028-bypass-followups.md
# coders: none named. Six small edits across five files that depend on each
#   other's tests; a handoff would cost more than it saves. The lead does all.
reviewers:
  - codex   # read-only final review of the diff against this plan
  - agy     # read-only second review (agy named by the owner for this run)
---

# Plan: close the follow-ups from the #1948 bypass reviews

Approved decisions (spec): (1) read base files through a hash-verified
commit -> tree -> blob walk, `ref` hex used as the commit oid directly;
(2) refuse any command word containing `/` unless it is an absolute path in a
fixed system dir; (3) `./build.sh` stays refused, pinned by a test, hint to use
`bash build.sh`; (4) parse Makefile rule lines with multiple targets, path
names and `include`; (5) yarn/pnpm/bun shorthand and `bun run <arg>` must name
a base script unless a `_RUNNER_BUILTINS` entry; (6) base not refreshed,
documented. All paths below are under `apps/backend/` except tests.

## Steps

1. `tests/test_base_ref_scripts.py` (append): tests that fail on dev: forged
   commit, tree and blob object under `.git/objects`; tag named like the base
   sha; `./echo`, `cd sub && ./echo`, `/tmp/echo`, `./build.sh` refused and
   `/usr/bin/env true`, bare `echo` allowed; Makefile `build.all lint: test`,
   `dist/app.js:`, `include common.mk` allowed, `X := a: b` and recipe lines
   not targets; `yarn echo` / `bun run echo` with agent-added root script
   refused while base scripts pass. → `pytest tests/test_base_ref_scripts.py`
   shows the new tests failing.
   Traps: tests use `AIFACTORY_GIT_BASELINE_DIR` fixture `repo`.
2. `project/config_parser.py:84-123`: add the verified walk (`_verified_tree`,
   bytes `cat-file` helper, hash check on raw bytes before any decode); a hex
   `ref` is the commit oid, a non-hex ref (`HEAD`) is resolved once with
   `rev-parse --verify`; walk `040000` subtrees (for `include sub/x.mk`),
   accept only `100644`/`100755` blobs (a `120000` symlink is not a file),
   sha1 and sha256 repos; route `read_text`, `file_exists`, `glob_files`
   through it when `ref` is set; refuse `..` and a leading `/` in paths.
   → step 1 object tests pass.
   Traps: keep `GIT_NO_REPLACE_OBJECTS`; decode utf-8 for text.
3. `project/structure_analyzer.py:88-107`: new rule-line parser (join `\`
   continuations, strip `#` comments, skip tab-indented recipes, `:`/`::` not
   followed by `:` or `=`, skip `$`/`%` names) and `include`/`-include`/
   `sinclude` following with several operands (depth 5, plain names only). →
   Makefile tests pass.
4. `security/hooks.py` (near `_validator_refusal`): `_path_command_refusal`
   reads the first token of each segment (the original word, since `name` is
   already a basename), runs BEFORE the `is_command_allowed` loop in both
   `bash_security_hook` and `validate_command`, so its `bash build.sh` hint is
   what the agent sees. A word with `/` passes only if it is absolute, has no
   `..` component, and `posixpath.normpath` leaves its directory in the system
   set. Add `/usr/bin/../../tmp/echo` to step 1's tests. →
   path tests pass; `tests/test_security.py`, `tests/test_command_sandbox.py`
   unchanged green.
5. `security/script_validators.py:177-227`: drop `_is_worktree_script`. In
   `_target_script` for yarn/pnpm/bun: `bun run <arg>` returns `<arg>` (always
   a base-script check, never a built-in exemption: `bun run test` runs the
   script), bare `bun run` returns None; shorthand `<tool> <name>` returns
   None when `name` is in `_RUNNER_BUILTINS[tool]` or is an
   `_RUNNER_SUBCOMMANDS[tool]` entry (`exec`, `dlx`, `x`) or bun `exec`, else
   `<name>`. → runner tests pass, `pnpm exec`, `yarn dlx`, `bun x` still pass.
   Traps: `ast_parser` does not import the validators, so import its two
   private tables at module level.
6. `core/git_metadata.py` `record_base_sha`: comment that the base is not
   refreshed by design (spec item 6). No behaviour change.

## Tests

`cd apps/backend && pytest ../../tests/test_base_ref_scripts.py
../../tests/test_security*.py ../../tests/test_command_sandbox.py -q` all
green; `ruff check` and `ruff format --check` on touched files (no `as` aliases
in parenthesised imports).

## Rollback

Revert the PR. No data, profile format or config changes.
