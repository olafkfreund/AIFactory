---
status: draft
issue: 1944
spec: spec/2026-10-27-1944-untrusted-profile.md
---

# Plan: the Bash hook must not take its allowlist from a file the agent can write

Approved decisions (from the spec):

- The hook's profile is an in-process object per `(project_dir, spec_dir)`, built once
  by fresh detection (`force_reanalyze=True`). It is never loaded from
  `.aifactory/.aifactory-security.json`, and later file edits are ignored.
- Plan seeding mutates that same in-memory object; the file is written as output only.
- Defence in depth: file-tool writes to the profile file name are refused (Claude hook and
  `tools/executor.py`), and Bash commands whose text names it are refused.
- Option (a) chosen over re-validating the file on load (b): the agent can forge
  every field, not only `custom_commands`.

Paths are under `apps/backend/` unless they start with `tests/`.

## Steps

1. `security/profile.py` (whole cache section, ~17-100): replace the four
   `_cached_*` globals and the mtime logic with
   `_trusted_profiles: dict[tuple[Path, Path | None], SecurityProfile] = {}`.
   `get_security_profile(project_dir, spec_dir=None)` resolves both paths and returns the
   entry, or builds `get_or_create_profile(project_dir, spec_dir, force_reanalyze=True)`,
   stores it and returns it. One-line why comment (#1944: the file is agent-writable; it is
   output only, never an input). `reset_profile_cache()` clears the dict. Delete
   `_get_profile_mtime`, and delete `_get_profile_path` if it becomes unused (grep first).
   Update the docstrings.
   → verify: `pytest tests/test_plan_seed.py -q` (fails until step 2 lands, which is fine).
   Traps: `security/main.py` and `security/__init__.py` re-export names. Keep
   `get_security_profile` and `reset_profile_cache` exported, and grep for any import
   of a deleted name.

2. `project/analyzer.py` `seed_profile_with_plan_commands` (~483-486): replace
   `analyzer.load_profile()` / `analyze()` with
   `from security.profile import get_security_profile` (local import, like the existing
   `security.plan_commands` one) and `profile = get_security_profile(project_dir)`.
   Keep `analyzer.save_profile(profile)` (output only). Update the docstring paragraph
   that says the hook reads the file (it now shares the in-memory profile).
   → verify: `pytest tests/test_plan_seed.py tests/test_review_validation.py -q` green.

3. `security/filesystem_validators.py` (~175-194): add
   `protected_write_refusal(path: str, cwd: str | None) -> str | None`. It returns
   `git_metadata_write_refusal(path, cwd)`, or "Refused: the security profile is managed
   by AIFactory and cannot be written." when the lexical normpath or the resolved path has
   `.name.casefold() == ".aifactory-security.json"`. Fail closed on resolution errors like
   the git check. Use `ProjectAnalyzer.PROFILE_FILENAME` if importing it causes no import
   cycle; otherwise a module constant with a one-line comment naming the source.
   Then `security/hooks.py` `git_dir_write_hook` (~261-282) and `tools/executor.py`
   `_exec_write`/`_exec_edit` (~221, ~244) call `protected_write_refusal` in place of
   `git_metadata_write_refusal` (update their imports and the hook docstring: #1719, #1944).
   → verify: `pytest tests -q -k "git_dir or executor or git_metadata"` green.

4. `security/hooks.py` `bash_security_hook`: right after `command` is read and checked
   for emptiness (~168-170), block when `".aifactory-security.json" in command.casefold()`
   with the reason "Refused: the security profile is managed by AIFactory." Add the comment
   `# shortcut: text match only, an obfuscated path gets past it; enforcement no longer reads
   the file (#1944), so this is defence in depth.`
   → verify: ruff on the file.

5. Tests.
   - Rewrite `tests/test_security_cache.py`: its three mtime-invalidation tests assert the
     removed behaviour. Replace them with: a planted file before the first load is ignored
     (fresh detection; the planted command is not allowed), a file edit after the first load
     is ignored (same object returned), and `reset_profile_cache` forces a rebuild.
   - New `tests/test_profile_untrusted.py`:
     a) After the first `validate_command("ls", d)`, overwrite the profile file with
        `curl`, `wget`, `nc` in `base_commands` and in `custom_commands`:
        `validate_command` blocks each of them and still allows `ls`.
     b) `git_dir_write_hook` blocks Write and Edit to `.aifactory/.aifactory-security.json`
        (relative path, absolute path, upper-case variant, and a symlink `x.json` pointing
        to it), and allows `src/app.py`.
     c) `bash_security_hook` blocks `echo '{}' > .aifactory/.aifactory-security.json` and
        `cp x .aifactory/.aifactory-security.json`, and allows `ls`.
     d) Plan seeding grants `uv` and `validate_command("uv --version", d)` passes without
        a cache reset.
   → verify: `pytest tests/test_profile_untrusted.py tests/test_security_cache.py -q`. Test (a)
     must fail against origin/dev's `security/profile.py` (check with `git stash`-free method:
     copy the file aside, `git show origin/dev:apps/backend/security/profile.py > ...`, run,
     restore).
   Traps: call `reset_profile_cache()` at the start of every test; hooks are async, so use
   `asyncio.run` or `pytest.mark.asyncio` like the existing hook tests.

## Tests

```
cd apps/backend
.venv/bin/python -m pytest ../../tests/test_profile_untrusted.py ../../tests/test_security_cache.py ../../tests/test_plan_seed.py ../../tests/test_review_validation.py -q
.venv/bin/python -m pytest ../../tests -q -k "security or hook or profile or executor" 
```
Expected: all pass. Then ruff check, ruff format --check, and the mypy ratchet via pre-commit.

## Rollback

Revert the PR. The hook goes back to reading the file; there is no data migration.
