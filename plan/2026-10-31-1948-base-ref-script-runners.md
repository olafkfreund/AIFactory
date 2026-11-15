---
status: approved
issue: 1948
spec: spec/2026-10-31-1948-base-ref-script-runners.md
coders:
  1: agy
  2: agy
  3: agy
  4: agy
---

# Plan: script runners only from files tracked at the base ref

`coders`: the lane brief for the 2026-10-31 wave names agy for #1948. Claude
reviews each diff, runs the check and commits.

Decisions carried from the approved spec:
- Owner decision (#1932, option B): runners come only from files tracked at
  the base ref.
- The base sha is recorded at worktree creation, outside the worktree.
- Detection reads `Makefile`, `package.json`, `pyproject.toml`, root `*.sh`/
  `*.bash` and `.aifactory-allowlist` via `git show`/`git ls-tree` at that sha.
  - With no record it uses HEAD.
  - With no git work tree it reads the working tree, as today.
- New validators enforce make targets and npm/yarn/pnpm script names against
  the profile's detected lists. They refuse options that redirect to other
  files.
- #1968 spec step 2 (bwrap in the Job) is out. Its step 3 (docs) is in.
- Residuals are documented, not fixed: recipe and script body edits, lifecycle
  scripts, a bare `make` after reordering, and `bash x.sh`.

All paths below are under `apps/backend/` unless they start with `tests/` or
are `SECURITY.md`.

## Steps

1. `core/git_metadata.py` (after `record_worktree_baseline`, ~218): add
   `_base_sha_path(gitdir)`, `record_base_sha(cwd, run=None)` and
   `read_base_sha(cwd) -> str | None`.
   - `_base_sha_path(gitdir)` is `_baseline_path(gitdir + "\0base")`.
   - `record_base_sha` resolves the gitdir via `_REV_PARSE_DIRS`, gets
     `git rev-parse HEAD` with `_run_git`, and writes `{"base": sha}` with
     `_write_snapshot`.
   - `read_base_sha` returns None if the dir is not git, the file is missing,
     the JSON is bad, or the value does not match `^[0-9a-f]{40,64}$`.

   In `core/worktree.py` `create_worktree` (~871-879), call
   `record_base_sha(worktree_path)` inside the same `try`, after
   `record_worktree_baseline`.
   → verify by `.venv/bin/pytest ../../tests -q -k "worktree or git_metadata" -x`.
   Traps: `_run_git` uses `child_env()`. Do not change the existing baseline
   file formats.

2. `project/config_parser.py`: `__init__(self, project_dir, ref: str | None = None)`.
   - Add a private `_git(*args) -> str | None`: subprocess `git`, cwd
     `project_dir`, `env=child_env()`, timeout 30. It returns stdout, or None
     on a non-zero exit or OSError.
   - With `ref`, `read_text`/`read_json`/`read_toml` use
     `git show <ref>:./<filename>`. Parse errors still return None.
   - `file_exists` and `glob_files` match `fnmatch` against
     `git ls-tree --name-only <ref>` names. `glob_files` returns
     `project_dir / name`.
   - Without `ref`, behaviour is unchanged.

   In `project/structure_analyzer.py` `__init__`, build
   `ConfigParser(project_dir, ref=_base_ref(self.project_dir))`.
   `_base_ref` returns, in order:
   - `read_base_sha(dir)` (lazy import from `core.git_metadata`);
   - else `"HEAD"` if `git rev-parse --is-inside-work-tree` succeeds;
   - else None, with a one-line `shortcut:` comment saying non-git dirs are dev
     and tests only.

   With no record, it logs once at info level.
   → verify by `.venv/bin/pytest ../../tests/test_project_analyzer.py ../../tests/test_profile_untrusted.py ../../tests/test_security_refresh.py -q`.
   Traps:
   - Test fixtures that are git repos with uncommitted files may now see
     nothing. Fix the fixture (commit the file), not the code, and record it
     here.
   - Never `git add -A`.

3. New `security/script_validators.py` with `validate_make_command(seg)` and
   `validate_package_script_command(seg)`. Both return `(bool, str)`, use
   `shlex.split`, and load the profile with
   `security.profile.get_security_profile(Path(root))`, where `root` is
   `get_worktree_root()`. Refuse when `root` is None.

   Make rules:
   - `--` skips.
   - A long option whose name is in {`--file`, `--makefile`, `--directory`,
     `--eval`, `--include-dir`} refuses. Other long options skip.
   - A short option cluster containing any of `fCEI` refuses. Other short
     options skip.
   - Tokens with `=` skip. All-digit tokens skip.
   - Any other token is a target and must be in
     `profile.custom_scripts.make_targets`.
   - No targets: allowed only if `make_targets` is non-empty.

   npm/yarn/pnpm rules:
   - Refuse option names in the spec's location set.
   - Skip other options. For `--loglevel` and `--registry` without `=`, also
     skip the value.
   - The first positional is the subcommand. Refuse `workspace`, `workspaces`,
     `recursive`, `multi` and `m`.
   - For `run`/`run-script`/`rum`/`urn`, the next positional (after options) is
     the script. None means allowed (it lists the scripts).
   - For `test`/`t`/`tst`, the script is `test`. For `start`/`stop`/`restart`,
     it is the subcommand itself.
   - The script must be in `profile.custom_scripts.npm_scripts`.
   - Any other subcommand is allowed.

   Each refusal names the target or script and says it is not in the base
   commit's file.

   Register `"make"`, `"npm"`, `"yarn"` and `"pnpm"` in
   `security/validator_registry.py` `VALIDATORS`.
   → verify by `.venv/bin/pytest ../../tests/test_security.py -q`.
   Traps:
   - Import `security.profile` inside the function. `profile` imports
     `project_analyzer`, which imports `security.plan_commands`.
   - Validators run after `is_command_allowed`, on unwrapped segments.

4. `tests/test_base_ref_scripts.py` (new). Fixture:
   - `tmp_path` git repo (`git -c user.email=t@t -c user.name=t commit`), with
     `monkeypatch.setenv("AIFACTORY_GIT_BASELINE_DIR", tmp/"bl")` and
     `reset_profile_cache()`.
   - Base commit: `Makefile` (`test:` and an echo recipe),
     `package.json` `{"scripts":{"test":"echo ok"}}`, and `build.sh`.
   - Call `record_base_sha(repo)`.
   - Then add `evil` to both files, commit, and write an untracked `evil.sh`.

   Assert with `security.hooks.validate_command`:
   - allowed: `make test`, `npm test`, `npm run test`;
   - refused: `make evil`, `npm run evil`, `yarn run evil`, `pnpm run evil`,
     `make -f x.mk test`, `npm --prefix sub run test`, `./evil.sh`;
   - `get_security_profile(repo).custom_scripts.shell_scripts == ["build.sh"]`.

   Add a second test: a repo with no Makefile at base, where the agent writes
   one with `evil`. Assert `"make" not in profile.script_commands`, and that
   `make evil` is refused.
   → verify by `.venv/bin/pytest ../../tests/test_base_ref_scripts.py -q`.
   Then show that it fails without the fix: run it on `git stash`-free
   origin/dev code, via `git worktree` of origin/dev or by reverting steps 1-3
   in a scratch copy. Report the result.

5. Docs, done by Claude:
   - `apps/backend/security/hooks.py` module docstring: add the guardrail
     paragraph.
   - `SECURITY.md`: add a section "Agent command allowlist" after Scope. It
     says the allowlist, validators and egress check are guardrails, the Job
     pod is the boundary (#1932 option B), and lists the residuals from the
     spec.

   → verify by `ruff check` and `ruff format --check` on the touched files.

## Deviations

- Step 1: `create_worktree` passes `record_base_sha` a `run=` that goes through
  `self._run_git(..., cwd=worktree_path)`, like `record_baseline` above it, so
  an ambient `GIT_DIR` is stripped (`test_create_worktree_ignores_ambient_git_dir`).
- Step 2: `ConfigParser` exposes the git helper as `git_output` (public, so
  `_base_ref` reuses it instead of a second subprocess call). `read_json` now
  goes through `read_text` in both modes. Both changes came from the ruff and
  mypy ratchets.
- Step 3 (Claude, after review): the npm location options are refused only
  when a script would run, so `npm install -w pkg` keeps working. Their
  values are consumed, so `npm -w pkg test` is still seen as `test`.
- Step 2 (Claude, after review round 2): `git_output` runs git with
  `GIT_NO_REPLACE_OBJECTS=1`. The allowlist lets the agent run `git replace`
  or `git update-ref refs/replace/<base>`, which otherwise makes
  `git show <base>:./Makefile` return the agent's file. Pinned by
  `test_replace_refs_cannot_swap_base` (both forms).
- Steps 1-4 (Claude, 2026-11-14, after the codex bypass review; every item
  has a case in `tests/test_base_ref_scripts.py` that fails without it):
  - Step 1: the base sha is also recorded under the worktree path, and
    `read_base_sha` falls back to the record of the directory or its nearest
    recorded parent. A nested `git init sub` or
    `git init --separate-git-dir` gave a gitdir with no record, so detection
    fell back to the agent's HEAD.
  - Step 3, make: a long option is refused when it is a prefix of a refused
    name (GNU make accepts `--fil=x.mk`). The variables `SHELL`,
    `.SHELLFLAGS`, `MAKEFILES`, `MAKEFLAGS` and `GNUMAKEFLAGS` are refused on
    the command line. An all-digit token is skipped only after `-j`/`-l` or
    their long forms; otherwise it is a target (`make 123`).
  - Step 3, packages: `it`, `install-test`, `cit`, `install-ci-test`,
    `clean-install-test` and `sit` count as running `test`. `--script-shell`
    is a refused option. `yarn <name>` and `pnpm <name>` are checked when
    `<name>` is a script in the working tree's `package.json`. `bun` is
    registered: `bun run <name>` and `bun <name>` are checked the same way
    (`bun test` is bun's own runner and is not).
  - `security/ast_parser.py` (not in the plan's file list): the env-key
    refusal now covers `MAKEFLAGS`, `MAKEFILES`, `GNUMAKEFLAGS` and any
    `npm_config_*` name, in any case. The validators never see env prefixes,
    `env VAR=…` or `export`, so this is the one place that does.
  - Not fixed, added to the residuals in `SECURITY.md`: an agent-written
    `GNUmakefile`/`makefile`, and `cd sub && make test`. The target name is
    still one from the base commit, so both equal an edited recipe body.
- Step 3 (Claude, 2026-11-14, after the Fable review; each has a test):
  - make: after `--` every word is a target (`make -- -evil`). `-o`/`-W` and
    their long forms are refused, since they take a file and hid the next
    word (`make -o -j 123`). Variable names are stripped of spaces
    (`'SHELL := x'`), and `.DEFAULT_GOAL` is refused. `-l 2.5` is a number.
  - npm: an unambiguous prefix of `run-script`, `test`, `start`, `stop`,
    `restart`, `install-test` or `install-ci-test` counts as that command
    (`npm run-s evil`). Prefixes of `install` stay `install`.
  - yarn/pnpm/bun shorthand: a working-tree `package.json` that Python cannot
    parse strictly (BOM, trailing comma) counts as defining the name, and
    every duplicate `scripts` object is read. The runners parse more loosely.
- Step 3 (Claude, 2026-11-14, Fable review round 2; each has a test):
  - make: a count is ASCII digits after `-j`, or a decimal after `-l`
    (`make -j 2.5` and `make -j ²` ran targets). A variable with the `!=`
    operator or `$` in its value is refused (`X!=cmd`, `X=$(shell cmd)`).
    `-O<type>` is no longer read as an option cluster.
  - npm: the prefix match covers every alias (`npm ur x`, `npm ts`), and
    leaves `install`, `ci`, `clean-install` and `star` alone.
- Step 3 (Claude, 2026-11-14, Fable review round 3; each has a test). This
  replaces the plan's make rules "other long options skip", "other short
  options skip" and "tokens with `=` skip": make arguments are now accepted
  by shape instead of refused by name. A word must be one of:
  - a target in the base list;
  - a short cluster of no-value letters, optionally ending in `-j[N]` or
    `-l[N]`, or `-O[type]`;
  - a long option from a fixed list that reads no file, spelled in full;
  - a count after a bare `-j`/`-l` or its long form;
  - `NAME=value` (`=`, `:=`, `::=`, `+=`) with a literal value and a name
    other than `SHELL`, `MAKEFILES`, `MAKEFLAGS`, `GNUMAKEFLAGS`.

  The refuse-lists kept losing to rewriting by make or the shell
  (`make -lj 123`, `make '$(shell x)V=1'`, `make $A=x`, `make -$A`,
  `make {SHELL,V}=x`, `make SHEL?=x`). Cost: `make V=$(git rev-parse HEAD)`
  and abbreviated long options are refused.
- `security/ast_parser.py` (Claude, 2026-11-14, Fable review round 4, with
  tests): an environment name that is not a plain identifier is refused, so
  `env .SHELLFLAGS=… make test` and `env .EXTRA_PREREQS=evil make test` no
  longer reach make. No reviewer re-checked this last change.
- The autonomy matrix (`docs/docs/compliance/autonomy-matrix.md`,
  `docs/static/compliance/autonomy-matrix.json`) was regenerated because CI
  checks it against the validator registry. No step named it.
- Known and not fixed here, filed as follow-ups: a forged loose object in a
  gitdir the agent controls can stand in for the base commit; detection sees
  only the first target on a Makefile rule line and no `include`.

## Tests

From `apps/backend`:
- `.venv/bin/ruff check` and `ruff format --check` on the touched .py files;
- `.venv/bin/python ../../scripts/ratchet_lint.py`, if present;
- `.venv/bin/pytest ../../tests/test_base_ref_scripts.py ../../tests/test_security.py ../../tests/test_project_analyzer.py ../../tests/test_profile_untrusted.py ../../tests/test_security_refresh.py -q`;
- the pre-commit hook suite on each commit.

## Rollback

Revert the PR. The base-sha record files are inert without the readers, so
leaving them in git-baselines is harmless.
