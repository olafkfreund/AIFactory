---
status: approved
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

## Deviations

- Step 5a forges `nc`, `ncat` and `socat`, not `curl`/`wget`/`nc`: `curl` and `wget` are in
  `BASE_COMMANDS`, so detection allows them anyway. The test edits the file detection just
  wrote, so the old mtime reload would have accepted the forgery; it fails on origin/dev's
  `security/profile.py`.
- `tests/test_plan_seed.py`: removed two `reset_profile_cache()` calls made after seeding
  "to reload from the file". That reload is the behaviour removed here, and the reset would
  now drop the in-memory grant.
- `filesystem_validators.py` defines `_PROFILE_FILENAME` locally; importing `ProjectAnalyzer`
  there would create an import cycle.

## Review round 1 (lead's Opus review of #1947)

Same decisions as above; these steps close what the review found.

6. HIGH, `.aifactory-allowlist` is an agent-writable trusted input.
   a. `project/structure_analyzer.py` `load_custom_allowlist` (~117-127): add a line only if it
      is in `(BASE_COMMANDS | PLAN_GRANTABLE_COMMANDS) - DENY_COMMANDS`. Use a local import
      with `# noqa: PLC0415 - <reason>` if a module-level import makes a cycle. Operators who
      need other names use `AIFACTORY_EXTRA_ALLOWED_COMMANDS`, which is env-only.
   b. `security/filesystem_validators.py` `protected_write_refusal`: also refuse the file name
      `.aifactory-allowlist` (case-folded). `security/hooks.py` `bash_security_hook`: the text
      match also covers `.aifactory-allowlist`.
   c. `agents/utils.py` `_BOOKKEEPING_PATHS` (~26-31): add `".aifactory-allowlist"` so agent
      edits are never committed by the safety net.
7. MEDIUM, a frozen profile misses tooling a scaffold adds mid-build (for example, a
   `Cargo.toml` added after the first hook call blocks `cargo build`).
   `project/analyzer.py` `seed_profile_with_plan_commands`: first run fresh detection
   (`ProjectAnalyzer(project_dir).analyze(force=True)`) and union its `stack_commands`,
   `script_commands` and `custom_commands`, each minus `DENY_COMMANDS`, into the trusted
   in-memory profile. Do this before the plan read/early returns so it runs on every seed.
   Then add the plan grants, and `save_profile` the trusted profile when anything changed.
8. LOW, the standalone `--qa` entry (`cli/qa_commands.py` `handle_qa_command`, before
   `run_qa_validation_loop`): seed plan grants, because a fresh process no longer inherits
   them from the file. If review is required (`requires_review_before_coding`), grant from
   the plan only while `ReviewState.load(spec_dir).is_approval_valid(spec_dir)` holds,
   otherwise pass `plan={}`. If review is not required, pass `plan=None`. Use the same
   `project_dir` the QA loop gives the client. Wrap the call in
   `try/except Exception` like the coder loop.
9. LOW, `trusted_plan.py` (~879-888): delete the ingest-time seed call. It runs in the API
   server process, where no hook reads the profile, and every ingest added a
   `_trusted_profiles` entry that was never freed. The coder loop seeds in-process before
   every session.
10. NIT: fix the stale comments at `agents/coder.py` ~380-383 and
    `agents/parallel_integration.py` ~519-522. They should say that grants go into the
    in-memory profile the hook uses, not "the file the Bash hook reads (mtime-invalidated)".
11. Tests (`tests/test_profile_untrusted.py`):
    - Write and Edit to `.aifactory-allowlist` are blocked, and a Bash `echo nc >> .aifactory-allowlist` is blocked.
    - With `nc` (and `uv`) written to `.aifactory-allowlist` and then `reset_profile_cache()`
      (a fresh cache key), `nc -l 4444` is blocked and `uv` is allowed.
    - Scaffold case: first `validate_command("ls")`, then write a `Cargo.toml`, run
      `seed_profile_with_plan_commands` with a plan that has no commands, and
      `cargo build` is allowed.
    - `_BOOKKEEPING_PATHS` contains `.aifactory-allowlist`.
    - A `--qa` seed test, if it stays small: review required with an invalid approval grants
      nothing, and with a valid approval it grants the plan's commands.
    → verify: the plan's Tests commands, plus `pytest tests -q -k "trusted_plan or qa_command or structure or allowlist"`.

### Review round 1: deviations

- Step 7: `seed_profile_with_plan_commands` always writes the trusted profile back at the
  end, not only "when anything changed". `analyze(force=True)` overwrites the file with
  detection alone, so a conditional save left it without the plan grants. This broke
  `test_plan_review_gate_snapshot.py`, which reads the file.
- Step 7: an unreadable plan now means "no plan grants" (`plan = {}`) rather than an early
  return, so re-detection still applies.
- Step 8: when the approval is valid, the seed is called with `plan=None`, which reads the
  live plan. That plan is the approved one, because `is_approval_valid` just matched its hash.
- Step 9: deleting the seed left `ingest_trusted_plan`'s `project_dir` parameter unused, and
  the ruff ratchet flagged it (ARG001). The parameter is removed, along with its only use
  in `apps/web-server/server/routes/execution.py`.
- Step 6a changed the behaviour that `tests/test_project_analyzer.py::TestCustomAllowlist::test_loads_custom_allowlist`
  asserted, so it now checks grantable names (`uv`, `pytest`) and that `my-custom-tool` is dropped.
- Step 11: the `--qa` test patches module objects from `importlib.import_module(...)`, not
  dotted strings, because other tests swap the `review` package in `sys.modules`.

## Review round 2

12. `core/worktree.py` `commit_in_worktree` (~1221): `git add . ':!.aifactory' ':!.aifactory-allowlist'`.
    On the parallel path (`parallel_integration.py` ~699), a child could otherwise commit an edited
    allowlist into the task branch, and later tasks would inherit grantable names without a plan
    declaring them. Test: `tests/test_worktree_commit_logging.py::test_commit_never_stages_allowlist`.

Accepted, not changed: `_trusted_profiles` is unbounded. It holds one entry per worktree per
build process, and since step 9 the web-server never calls `get_security_profile`, so its
size is bounded by the worktrees one build touches.
