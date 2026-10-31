---
status: draft
issue: 1948
intent: intent/2026-10-31-1948-base-ref-script-runners.md
---

# Spec: script runners only from files tracked at the base ref

## Design

User decision (owner, #1932, 2026-10-31, option B step 1): runners only from
files tracked at the base ref. This supersedes the rejection of base-ref
pinning (alternative A) in `spec/2026-10-30-1948-indirect-exec-boundary.md`.
It is now a gate-hardening layer under the Job boundary, not a claim of
containment.

### 1. Record the base sha at build start

`apps/backend/core/git_metadata.py` gets `record_base_sha(cwd)` and
`read_base_sha(cwd)`. The record is `{"base": <HEAD sha>}`, written with the
existing `_write_snapshot` to `_baseline_path(gitdir + "\0base")`. That path is
in the git-baselines dir (`~/.aifactory/git-baselines` or
`AIFACTORY_GIT_BASELINE_DIR`), outside the worktree, keyed by the worktree's
own git dir. `core/worktree.py` `create_worktree` calls `record_base_sha` next
to `record_worktree_baseline`. That covers the subprocess and kubejob backends,
because both create the worktree there (worktree.py ~354). `read_base_sha`
returns None when the file is missing or the value is not a hex sha.

### 2. Detection reads the base ref

`project/config_parser.py` `ConfigParser(project_dir, ref=None)`:
- With `ref`, `read_text`/`read_json`/`read_toml` read
  `git show <ref>:./<file>` instead of the file.
- `file_exists`/`glob_files` match names from `git ls-tree --name-only <ref>`.
- A git failure reads as "file absent", which is fail-closed.

`StructureAnalyzer` builds its parser with `ref = read_base_sha(dir)`. It falls
back to `HEAD` when no record exists (direct mode, spec creation in the project
dir), and to `None` (today's working-tree read) only when the dir is not a git
work tree. `.aifactory-allowlist` is read through the same parser, so it also
comes from the base ref. Stack detection (`StackDetector`, `FrameworkDetector`)
is unchanged: it grants toolchain names, not scripts.

### 3. Enforce targets, not only runner names

New `security/script_validators.py`, registered in
`security/validator_registry.py`:
- `make`: each target must be in `profile.custom_scripts.make_targets`.
  - Refused: `-f`, `-C`, `-E`, `-I` and `--file`, `--makefile`,
    `--directory`, `--eval`, `--include-dir`. They point make at other files.
  - Skipped: `VAR=value` tokens and bare numbers (`-j 4`).
  - A bare `make` is allowed when the base has at least one target.
- `npm`, `yarn` and `pnpm`: the script of `run`/`run-script` (and npm's `rum`
  and `urn`), and of `test`/`t`/`tst`/`start`/`stop`/`restart`, must be in
  `profile.custom_scripts.npm_scripts`.
  - Refused: options that move where the package.json is read from (`--prefix`,
    `-C`, `--cwd`, `--dir`, `--filter`/`-F`, `-w`/`--workspace`, `-ws`/
    `--workspaces`, `-r`/`--recursive`, `--workspace-root`,
    `--include-workspace-root`).
  - Also refused: the yarn/pnpm `workspace`/`workspaces`/`recursive`/`multi`/
    `m` prefixes.
  - Other subcommands pass through (`npm install`, `npm ci`).
- The profile comes from `get_security_profile(get_worktree_root())`, which is
  the hook's own cached profile. With no root, the validator refuses.

Poetry/PEP 621 scripts and `*.sh` names are derived from the base ref by
step 2. Nothing grants on them today: `./x` reaches the allowlist as `x`, so
no enforcement change is needed there.

### 4. Document the guarantee (#1968 spec step 3)

`security/hooks.py` module docstring and a short `SECURITY.md` section say:
- the allowlist, validators and egress check are guardrails, not containment;
- the containing boundary is the Job pod (#1932 option B).

They also name the residuals in Risks.

## Alternatives rejected

- **Leave it to the sandbox (#1968 spec, alternative A rejected).** Overruled
  by the owner on 2026-10-31. The pod boundary contains the residual, and this
  closes the cheap path.
- **#1968 spec step 2 (bwrap around the kubejob run.py).** Under RuntimeDefault
  seccomp bwrap cannot create a namespace and fails open. The owner chose no
  custom seccomp profile.
- **Base = `git merge-base HEAD origin/<base>` at hook time.** The hook knows
  only cwd, not the base branch. The agent can also move refs.
- **Base = HEAD at first analysis.** Includes agent commits from earlier
  sessions or resumed builds.
- **Pin recipe and script content too.** That would refuse every build that
  legitimately edits a target. The surface is unbounded (recipes call other
  files).
- **Fail closed in non-git dirs.** Builds always run in a git worktree. Non-git
  dirs are local tests and dev only. Today's behaviour is kept there.

## Risks

- **Residuals, contained by the Job pod and not by this change:**
  - the agent edits the recipe of a tracked target, or the body of a tracked
    script;
  - npm `pre<name>`/`post<name>` and install lifecycle scripts;
  - reordering the Makefile so that a bare `make` runs a new first target;
  - `bash x.sh`, interpreters, and test runners.
- **A build that adds a target and then runs it is refused.** That is the
  intent. The agent can run the recipe's commands directly, and each is checked.
- **Same-uid write to the git-baselines dir.** The record has the same trust as
  the existing #1746 baseline. Option B puts that dir in the Job pod.
- **A worktree created before this change has no record.** It falls back to
  HEAD, which includes committed agent work, and logs that.

## Verification

- New `tests/test_base_ref_scripts.py`, on a git repo whose base commit has
  `Makefile` `test`, `package.json` `test` and `build.sh`. The agent then adds
  `evil` (committed), `evil.sh`, and a `Makefile` in a fresh repo:
  - allowed: `make test`, `npm test`, `npm run test`;
  - refused: `make evil`, `npm run evil`, `yarn run evil`, `pnpm run evil`,
    `make -f other.mk test`, `npm --prefix sub run test`, `./evil.sh`;
  - `shell_scripts == ["build.sh"]`.
- The test fails on origin/dev, where `make evil` and `npm run evil` are
  allowed.
- Existing `tests/test_security.py`, `test_project_analyzer.py`,
  `test_profile_untrusted.py`, `test_security_refresh.py` and worktree tests
  stay green.
