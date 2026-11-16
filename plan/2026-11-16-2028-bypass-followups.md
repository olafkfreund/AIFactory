---
status: draft
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
   bytes `cat-file` helper, hash check); route `read_text`, `file_exists`,
   `glob_files` through it when `ref` is set. → step 1 object tests pass.
   Traps: keep `GIT_NO_REPLACE_OBJECTS`; decode utf-8 for text.
3. `project/structure_analyzer.py:88-107`: new rule-line parser and include
   following (depth 5). → Makefile tests pass.
4. `security/hooks.py` (near `_validator_refusal`, called in
   `bash_security_hook` and `validate_command`): `_path_command_refusal`. →
   path tests pass; `tests/test_security.py`, `tests/test_command_sandbox.py`
   unchanged green.
5. `security/script_validators.py:177-227`: drop `_is_worktree_script`, use
   `_RUNNER_BUILTINS`; `bun run <arg>` base check. → runner tests pass.
   Traps: import `_RUNNER_BUILTINS` lazily if `ast_parser` imports validators.
6. `core/git_metadata.py` `record_base_sha`: comment that the base is not
   refreshed by design (spec item 6). No behaviour change.

## Tests

`cd apps/backend && pytest ../../tests/test_base_ref_scripts.py
../../tests/test_security*.py ../../tests/test_command_sandbox.py -q` all
green; `ruff check` and `ruff format --check` on touched files (no `as` aliases
in parenthesised imports).

## Rollback

Revert the PR. No data, profile format or config changes.
