---
status: approved
issue: 1944
intent: intent/2026-10-27-1944-untrusted-profile.md
---

# Spec: the Bash hook must not take its allowlist from a file the agent can write

## Design

**Authoritative profile in process memory (option a).** Hooks are in-process
callbacks in the build process (the SDK `HookMatcher` callables and
`tools/executor.py`), so the profile they use can live where the agent cannot
reach it.

- `security/profile.py`: replace the single-entry, mtime-invalidated cache with a
  `dict` keyed by `(resolved project_dir, resolved spec_dir)`. On a miss, build
  the profile with `get_or_create_profile(project_dir, spec_dir, force_reanalyze=True)`.
  That is fresh detection, so the file is never loaded. Later calls return the same
  object, and edits to the file are ignored. `reset_profile_cache()` clears the dict.
  The dict also stops parallel wave children (different worktrees in one process)
  from evicting each other.
- `project/analyzer.py` `seed_profile_with_plan_commands`: take the profile from
  `security.profile.get_security_profile(project_dir)`, the same in-memory object
  the hook uses, instead of `load_profile()`/`analyze()`. Add the grants to it,
  then `save_profile` as output only. Grants apply immediately and never pass
  through the file.
- Write guard (defence in depth): `security/filesystem_validators.py` gets
  `protected_write_refusal(path, cwd)`. It returns the existing `.git` refusal,
  or a refusal when the lexical or resolved path's file name is the profile
  file name (case-folded). `git_dir_write_hook` (`security/hooks.py`) and
  `tools/executor.py` (Write/Edit) call it in place of `git_metadata_write_refusal`.
- Bash guard (defence in depth): `bash_security_hook` refuses any command whose
  text contains the profile file name (case-folded) before the allowlist check.
  The limit goes in a `shortcut:` comment: an obfuscated name gets past it, which
  is acceptable because the file is no longer trusted.

Why (a) over (b): re-validating on load would have to re-derive every field
(base/stack/script/custom), because the agent can write any of them. That means
re-running detection anyway, plus a per-field policy. With (a) the profile is
detection plus in-process grants and nothing else, by construction.

Behaviour change: detection runs once per process per worktree. Today the hook
re-detects only by accident, when a seeding write bumps the file mtime, and that
re-detection also drops plan grants until the next seed. The plan-grant mechanism
already covers tooling a scaffold adds mid-run (see the docstring of
`seed_profile_with_plan_commands`).

## Alternatives rejected

- (b) Filter `custom_commands` against `DENY_COMMANDS`/`PLAN_GRANTABLE_COMMANDS`
  on load: leaves `base/stack/script_commands` forgeable, and would need a
  per-field allowed-source policy.
- HMAC-sign the file with a per-process key: more code than (a) for the same result.
- Write and Bash guards alone: the shell has too many write paths (variables,
  interpreters, globs) for a text check to be a boundary.

## Risks

- Grants written to the file by another process (`trusted_plan.py` at ingest)
  are no longer seen by the build process. The coder loop seeds again in-process
  before every session, so the commands are still granted.
- Detection now runs once per build process instead of reusing a cached file.
  This costs one analysis per worktree per process.
- `tests/test_security_cache.py` asserts mtime invalidation, which is removed on
  purpose. Those tests are rewritten to assert the new contract.

## Verification

New `tests/test_profile_untrusted.py`:
- Write `curl`/`wget`/`nc` into the profile file (any field) after the first
  load: `validate_command` still blocks them. Base commands (`ls`) stay allowed.
- A profile file planted before the first load is ignored (fresh detection).
- `git_dir_write_hook` blocks Write/Edit to `.aifactory/.aifactory-security.json`
  (relative, absolute, and via a symlink). Normal files are still allowed.
- `bash_security_hook` blocks `echo … > .aifactory/.aifactory-security.json` and
  `cp x .aifactory/.aifactory-security.json`.
- Plan seeding still makes `uv` allowed immediately (existing `test_plan_seed.py`).
Plus ruff, format, mypy ratchet, and the existing security test files.
