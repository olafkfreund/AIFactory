---
status: draft
issue: 1689
spec: spec/2026-10-10-1689-fsmonitor-git-config-audit.md
---

# Plan: agent-written git config must not run commands in the server

Worktree `/mnt/code/Source-home/GitHub/AIFactory-1689`, branch
`fix/1689-fsmonitor-git-config-audit`, planned against HEAD `d80f7b79`.

## Approval note (read before step 1)

The intent and spec frontmatter both say `status: approved`. The spec body
(line 21) still says "None of them is approved yet" and asks for explicit
confirmation of E1 and E2. This plan treats the spec approval as covering
Q1-Q5, E1 and E2. **The user confirms that when approving this plan.** If
E1 or E2 is not approved, drop step 5 (E1) or the E2 part of step 4 and the
test cases that belong to them.

## Approved decisions

- **Q1, read side.** `child_env()` pins `core.fsmonitor=false` next to the
  existing `core.hooksPath=/dev/null` pin. This covers every `child_env`
  caller: agent runner, builds, server git. Layout: hooksPath at index `n`,
  fsmonitor at `n+1`, `GIT_CONFIG_COUNT=n+2`. A caller's own `GIT_CONFIG_*`
  entries keep indices below `n`, so the pins come last and win. Do **not**
  pin `core.sshCommand`, `core.pager` or `core.editor`. They are residual
  risk with a follow-up issue.
- **Q2, write side.** Only in `validate_git`: a `git config` that sets a
  dangerous key is **refused** with a fail-closed message, following the
  pattern of the #321 C4 `-c` branch. Do not strip the key and do not just
  log it. Details:
  - Find the git subcommand. Skip leading `NAME=val` assignment tokens, then
    `git`, then global options. Bare `-C`, `-c`, `--config-env`,
    `--git-dir`, `--work-tree` and `--namespace` also consume the next
    token. Their `=` forms consume nothing.
  - If the subcommand is `config`, run `_is_dangerous_git_config` on
    `tok.split("=",1)[0]` for **every** non-option token after it, not only
    the first. Values are checked as well, so a value that matches the
    deny-list is refused (fails closed).
  - Refuse the edit and section modes `-e`, `--edit`, `edit`,
    `--rename-section`, `rename-section` and `--copy-section`.
  - In the unparseable (`shlex` `ValueError`) branch, also refuse when
    `" config "` is in `f" {lowered} "` and any whitespace-split word
    satisfies `_is_dangerous_git_config`.
  - Must stay allowed: `git config user.name/user.email`,
    `--get user.name`, `--local core.bare false`,
    `--global --add safe.directory <p>`, and `git commit -m config`. The
    server's own `git config` calls are subprocesses and never reach
    `validate_git`.
  - Not in scope: a Write/Edit `.git` guard and a server-side `.git/config`
    check. They wait for the #1672/#1673 pattern.
- **Q3.** Filter, textconv and diff drivers are documented residual risk.
  A follow-up adds `--no-ext-diff`/`--no-textconv` to server diff and
  `log -p` calls. **Never add `filter.*` or `lfs.*` to any list** (LFS,
  #1690).
- **Q4.** `core/worktree.py:_git_env()` is rebuilt as
  `child_env(keep=GITHUB_KEEP, extra={"GIT_TERMINAL_PROMPT": "0"})` minus
  every `_AMBIENT_GIT_VARS` key. It no longer copies `os.environ`.
  `core/worktree.py` joins `_BACKEND_FILES` in the spawn scan.
  `trusted_plan._git_subprocess_env` (`trusted_plan.py:568-592`) is
  unchanged: it intersects `child_env()` with the `_git_env()` keys, so it
  gives the same result (`GH_TOKEN` is in `_git_env` but not in the default
  `child_env`, so it stays excluded).
- **Q5.** The Job env allow-list in
  `apps/web-server/server/services/build_backend.py:259-300` is unchanged.
  The file does not change at all. Line 953
  (`child_env(keep=GITHUB_KEEP, extra={GIT_TERMINAL_PROMPT:'0'})`) is the
  push-path precedent that `_git_env` now mirrors. Lines 1086-1089 are the
  in-process `WorktreeManager`, which gets the pins through the new
  `_git_env`.
- **E1.** `hooks.py` validates **every** segment whose `extract_commands`
  contains the command, not only the first one. With no match it falls back
  to `[command]`, and it returns on the first refusal. This applies to all
  `VALIDATORS` (git, rm, chmod, ...). A new helper sits next to
  `get_command_for_validation` in `security/parser.py`, and both hook loops
  use it.
- **E2.** `_is_dangerous_git_config` adds: the `.program` suffix
  (`gpg.program`, `gpg.<fmt>.program`), the `credential.` prefix,
  `core.askpass`, `core.gitproxy`, `diff.external`, the `.driver` suffix,
  the `.uploadpack` and `.receivepack` suffixes, `include.path`, and the
  `includeif.` prefix. This also widens the existing `-c` block. `filter.*`
  stays unlisted.

### Rejected (do not implement)

- ssh, pager or editor pins now
- the literal first-token rule
- a Write/Edit `.git` guard or a server-side config check
- per-call `--no-ext-diff`
- pinning or denying `filter.*`
- forwarding `GIT_CONFIG_*` to Job pods
- keeping `_git_env` separate with only the pins added
- appending to `GIT_CONFIG_PARAMETERS`
- re-splitting `bash -c` inside `validate_git`

## Execution

There are 5 code steps that edit files, and 9 files are touched. Under the org
policy this goes to one `coder` agent: step 1 at start, then each later step
through `SendMessage`. A fresh Opus agent reviews the result, given only this
plan path and `git diff main`. Step 6 (CHANGELOG, follow-ups, PR) stays with
the session model.

Environment for every step:

```sh
export PATH=/mnt/code/Source-home/GitHub/AIFactory/apps/backend/.venv/bin:$PATH
cd /mnt/code/Source-home/GitHub/AIFactory-1689
T="tests/test_child_process_env.py tests/test_no_unscrubbed_spawn.py tests/test_command_sandbox.py tests/test_worktree.py tests/test_worktree_concurrent_lock.py tests/test_trusted_plan.py tests/test_sandbox_escape_corpus.py"
```

**Lint gate G**, run after every code step (stage the changes first):

```sh
ruff format --check apps/backend apps/web-server scripts tests && ruff check apps/backend apps/web-server scripts tests
python scripts/cq_ratchet.py --staged --ruff "$(command -v ruff)" --config standards/ruff.toml --paths 'apps/backend/*.py' 'apps/web-server/*.py' 'scripts/*.py'
python scripts/cq_ratchet.py --staged --tool mypy --mypy "$(command -v mypy)" --config standards/mypy.ini --paths 'apps/backend/*.py' 'apps/web-server/*.py' 'scripts/*.py'
python scripts/gen_autonomy_matrix.py --check   # baseline: ok: tiers=10 overlay=12 val=8 paths=28 gates=3 controls=13
```

If `gen_autonomy_matrix.py --check` fails, for example because the new
`worktree.py → core.child_env` import changes an import closure, run
`python scripts/gen_autonomy_matrix.py` and commit the regenerated
`docs/docs/compliance/autonomy-matrix.md` with the code.

Commit format: the scope may not contain `#`. Use for example
`fix(security): pin core.fsmonitor and refuse git config writes (#1689)`.
Every commit ends with the two attribution lines.

## Steps

1. **Red tests, tests only.** Commit as `test(security): ... (#1689)`.
   → verify by `python -m pytest $T -q`: the new and changed cases fail,
   every existing case stays green.

   - `tests/test_child_process_env.py:66-84`: edit both layout tests in
     place and keep their names.
     - `test_hooks_path_appended_after_existing_entry`: `COUNT=="3"`.
       KEY_0/VALUE_0 are still the credential helper. `KEY_1=="core.hooksPath"`,
       `VALUE_1=="/dev/null"`, `KEY_2=="core.fsmonitor"`, `VALUE_2=="false"`.
     - `test_hooks_path_is_entry_zero_without_count`: `COUNT=="2"`.
       `KEY_0=="core.hooksPath"`, `VALUE_0=="/dev/null"`,
       `KEY_1=="core.fsmonitor"`, `VALUE_1=="false"`.
     - Optional one line in `test_trusted_plan_git_env_is_scrubbed_and_headless`
       (about line 171): `assert env["GIT_CONFIG_KEY_1"] == "core.fsmonitor"`.
       This pins Q4's claim that `trusted_plan.py` needs no change.
   - `tests/test_child_process_env.py`, new test after line 84. It was
     checked against git 2.55: a hooksPath-only pin still runs the hook, and
     the two-key pin stops it.

     ```python
     def test_real_git_fsmonitor_disabled_by_child_env(secret_env: None, tmp_path: Path) -> None:
         iso = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
         base = {**os.environ, **iso}
         repo, wt = tmp_path / "repo", tmp_path / "wt"
         subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, env=base)
         subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t",
                         "commit", "-q", "--allow-empty", "-m", "i"], check=True, env=base)
         subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", str(wt), "-b", "w"], check=True, env=base)
         subprocess.run(["git", "-C", str(wt), "config", "core.fsmonitor", "touch M; false"], check=True, env=base)
         subprocess.run(["git", "-C", str(repo), "status"], env={**child_env(), **iso}, capture_output=True)
         assert not (repo / "M").exists()
         subprocess.run(["git", "-C", str(repo), "status"], env=base, capture_output=True)  # control
         assert (repo / "M").exists()
     ```

   - `tests/test_worktree_concurrent_lock.py`, next to 222-234 in the same
     class: add `test_git_env_scrubs_secrets_and_pins_hooks_and_fsmonitor(self, monkeypatch)`.
     - `setenv` `GIT_DIR=/x`, `DATABASE_URL=s`, `GH_TOKEN=t` and
       `GIT_TERMINAL_PROMPT=1`. `delenv` `GIT_CONFIG_COUNT`,
       `GIT_CONFIG_KEY_0` and `GIT_CONFIG_VALUE_0` with `raising=False`.
     - Assert that `GIT_DIR` and `DATABASE_URL` are absent,
       `env["GH_TOKEN"]=="t"` and `env["GIT_TERMINAL_PROMPT"]=="0"`.
     - Build `pins = {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"] for i in range(int(env["GIT_CONFIG_COUNT"]))}`
       and assert it contains `core.hooksPath: /dev/null` and
       `core.fsmonitor: false`.
     - The existing test at 222-234 (`GIT_AUTHOR_NAME` and `PATH` survive)
       is unchanged and stays green: `is_denied_env_key("GIT_AUTHOR_NAME")`
       is False.
   - `tests/test_no_unscrubbed_spawn.py:59-66`: add
     `_BACKEND / "core" / "worktree.py",` to `_BACKEND_FILES`, plus one
     comment line at 52-57 saying the in-process `WorktreeManager` runs in
     the server.
   - `tests/test_command_sandbox.py`:
     - Extend `GIT_RCE_PAYLOADS` (59-68), which `test_git_rce_options_blocked`
       runs through `validate_command`, with:
       `git config core.fsmonitor x`; `git config set core.sshCommand x`;
       `git -C d config core.pager x`;
       `git config -f .git/config core.fsmonitor x`;
       `git config --file=.git/config core.editor x`;
       `git config --global alias.x '!sh'`; `git config -e`;
       `git config --rename-section foo core`;
       `git status && git config core.fsmonitor x`;
       `git status && git -c core.fsmonitor=x status`;
       `git config gpg.program x`; `git -c credential.helper=x fetch`;
       `git config include.path x`.
     - Extend `LEGIT_COMMANDS` (79-83) with: `git config user.name "A B"`;
       `git config user.email a@b.c`; `git config --get user.name`;
       `git config --local core.bare false`;
       `git config --global --add safe.directory /w`; `git commit -m config`.
     - Add to `TestGitOptionHardening` (101-114). The pattern is the same as
       `tests/test_sandbox_escape_corpus.py:_bash`.

       ```python
       @pytest.mark.parametrize("payload", ["git status && git -c core.fsmonitor=x status",
                                            "git status && git config core.fsmonitor x"])
       def test_chained_git_segment_blocked_by_hook(self, payload, temp_dir):
           from security.hooks import bash_security_hook  # noqa: PLC0415
           out = asyncio.run(bash_security_hook(
               {"tool_name": "Bash", "tool_input": {"command": payload}, "cwd": str(temp_dir)}))
           assert out.get("decision") == "block", payload

       def test_unparseable_git_config_fails_closed(self, tmp_path, monkeypatch):
           monkeypatch.chdir(tmp_path)
           monkeypatch.setattr(git_validators, "_unstage_spec_artifacts", lambda: [])
           allowed, reason = git_validators.validate_git('git commit -m "x config core.fsmonitor y')
           assert allowed is False and "config" in reason
       ```

     - Add `import asyncio` and the plain module import
       `from security import git_validators`. Do not alias it or merge it
       into a multi-name import.

   Traps:
   - The whole of `test_command_sandbox.py` skips when bashlex is missing
     (line 27). Run it only with the venv on PATH, or a skipped run looks
     green.
   - The unparseable test calls `validate_git` directly, because
     `validate_command` already refuses unbalanced quotes through bashlex
     and would mask the case. It starts with `git commit` because other
     unparseable inputs already fail in `validate_git_commit` or in the
     existing ` -c ` branch. Without the fix the input reaches the commit
     path, which runs `_unstage_spec_artifacts` and `git diff --cached` in
     the cwd. The stub and `chdir` keep that away from the repo.
   - Run the scrubbed `status` before the control run. Otherwise `M` already
     exists.
   - The file header already has `# ruff: noqa: S603, S607, PLW1510`.
   - Every new legit case must pass the allowlist in `temp_dir` today. If
     one fails, it must be because of the new assertion, not because the
     allowlist blocks it.
   - The spawn-scan entry is green **before** the change too. `_is_environ`
     does not match the `{k: v for k, v in os.environ.items()}` comprehension
     at `worktree.py:98`. It only guards against future regressions. The
     `_git_env` test above is what catches a revert of Q4.
   - #1671/#1673 also edit `tests/test_child_process_env.py`, including the
     `secret_env` fixture and lines 66-84. Expect a rebase conflict.

2. `apps/backend/core/child_env.py:51-57` and docstring `:33-40`: Q1
   fsmonitor pin. → verify by
   `python -m pytest tests/test_child_process_env.py tests/test_trusted_plan.py -q`
   (all green), then G.
   - Keep the `n` computation (51-54) and hooksPath at `n` (55-56). Add
     `env[f"GIT_CONFIG_KEY_{n + 1}"] = "core.fsmonitor"` and
     `env[f"GIT_CONFIG_VALUE_{n + 1}"] = "false"`, then set
     `env["GIT_CONFIG_COUNT"] = str(n + 2)` in place of `str(n + 1)`.
   - Docstring line 38: "...and disables git hooks and fsmonitor via git
     env config. The pins are appended last (an existing `GIT_CONFIG_*`
     entry keeps its lower index)."

   Traps:
   - Keep the hunk inside 33-40 and 51-57. #1674/#1688 edit the strip-list
     and #1671 adds `RUNNER_KEEP` in this file. #1674/#1688/#1692 must
     rebase onto `COUNT=n+2`.
   - Do not pin `core.sshCommand`, `core.pager` or `core.editor`.
   - git below 2.36 ignores the `false` value and tries to run `false` as a
     hook, which is `/bin/false`: harmless.

3. `apps/backend/core/worktree.py:17-29` (imports) and `:90-100`
   (`_git_env`): Q4 rebuild. → verify by
   `python -m pytest tests/test_worktree.py tests/test_worktree_concurrent_lock.py tests/test_no_unscrubbed_spawn.py tests/test_trusted_plan.py -q`
   (all green). `git diff main -- apps/web-server/server/services/build_backend.py`
   must print nothing. Then G.
   - Import: `from core.child_env import GITHUB_KEEP, child_env`, on one
     line, unaliased, in its own first-party block after the stdlib block.
     Do not use `from core import child_env`, because `core/__init__.py`
     has a lazy loader. There is no import cycle: `core.child_env` imports
     `core.auth`, and `core.auth` does not import `worktree`.
   - Body:

     ```python
     env = child_env(keep=GITHUB_KEEP, extra={"GIT_TERMINAL_PROMPT": "0"})
     for k in _AMBIENT_GIT_VARS:
         env.pop(k, None)
     return env
     ```

     Delete the `os.environ.items()` copy at line 98.
   - Docstring: host secrets are scrubbed through `child_env`, the GitHub
     keep-list is kept, the hooksPath and fsmonitor pins are applied,
     ambient `GIT_*` vars (`_AMBIENT_GIT_VARS`, 79-87) are dropped, and the
     headless prompt reason (#1106) stays.
   - Keep `import os`: line 56 still uses `os.getenv`.
   - The 9 spawns at 154/164, 420/426, 479/486, 489/496, 522/529, 543/550,
     571/578, 597/604 and 629/637 already pass `env=_git_env()` and do not
     change.
   - No change to `trusted_plan.py:568-592` or `build_backend.py`.

   Traps:
   - `WorktreeManager` now runs with `hooksPath=/dev/null`, so project
     pre-commit and merge hooks no longer run there. This is an intended,
     stated risk.
   - `*_KEY`/`*_TOKEN` variables outside `GITHUB_KEEP` are dropped. A GHE,
     GitLab or CodeCommit `gh` helper now fails loudly at the spawn at
     `worktree.py:420-428`. The spec's ":410-413" is the method
     def/docstring.
   - `pr_endgame.py:967` (`-c core.editor=true rebase --continue`) is
     unaffected.

4. `apps/backend/security/git_validators.py:31-48`, `:57-66` and `:68-89`:
   Q2 and E2 validator. → verify by
   `python -m pytest tests/test_command_sandbox.py -q`. Everything passes
   except the `git status && git config ...` cases and the
   `test_chained_git_segment_blocked_by_hook` cases, which go green in
   step 5. Then G.
   - E2, 31-48: add to the `or` chain
     `k.endswith(".program")`, `k.startswith("credential.")`,
     `k in {"core.askpass", "core.gitproxy", "diff.external", "include.path"}`,
     `k.endswith(".driver")`, `k.endswith((".uploadpack", ".receivepack"))`
     and `k.startswith("includeif.")`. Update the docstring (32-33) to name
     the new families. **Do not add `filter.*` or `lfs.*`** (#1690).
   - Q2, unparseable branch 57-66: before the fallback
     `return validate_git_commit(...)`, refuse when
     `" config " in f" {lowered} "` and
     `any(_is_dangerous_git_config(w) for w in lowered.split())`. Use a
     fail-closed message such as "git blocked: `git config` on a key that
     can execute a command, and the command could not be parsed for
     validation."
   - Q2, new private helper `_git_config_refusal(tokens: list[str]) -> str | None`,
     called after the token loop (which ends at 87) and before
     `return validate_git_commit(command_string)` (89). If it returns a
     reason, return `(False, reason)`.
     - Skip leading assignment tokens
       (`re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", t)`), because
       `validate_git` can receive a segment like `GIT_EDITOR=x git config -e`.
       Then skip `git`.
     - Skip tokens starting with `-`. The bare forms of `-C`, `-c`,
       `--config-env`, `--git-dir`, `--work-tree` and `--namespace` also
       consume the next token. Their `=` forms do not.
     - If the first non-option token is `config`, for each later token:
       refuse if it is in
       `{"-e", "--edit", "edit", "--rename-section", "rename-section", "--copy-section"}`.
       For a token not starting with `-`, refuse if
       `_is_dangerous_git_config(tok.split("=", 1)[0])` is true.
     - Use the message pattern of the `-c` branch at 79-82, e.g.
       ``f"git blocked: `git config` on `{key}` can execute an arbitrary command (config injection). Not permitted."``
       and for edit modes
       ``f"git blocked: `git config {tok}` can rewrite config to execute an arbitrary command. Not permitted."``.

   Traps:
   - Refuse. Do not strip the key or only log it.
   - Values are checked too, so `git config user.name foocommand` is refused
     (`endswith("command")`). That is an accepted false positive.
   - `commit.gpgsign` and `safe.directory` must stay allowed.
     `test_benign_git_config_allowed` (about line 111) guards this.
     `.program` must not match `commit.gpgsign`, and it does not.
   - The subcommand parse is what keeps `git commit -m config` allowed. Do
     not use `"config" in tokens`.
   - Add `import re` only if it is not already imported.

5. `apps/backend/security/parser.py:121-129`, `apps/backend/security/hooks.py:24`,
   `:187-199` and `:245-257`: E1, validate every segment. → verify by
   `python -m pytest $T tests/test_security*.py -q` (all green), then
   `python -m pytest tests -q` (no new failures against main), then G.
   - `parser.py`, after `get_command_for_validation` (121-129), add:

     ```python
     def get_segments_for_validation(cmd: str, segments: list[str]) -> list[str]:
         """Every segment whose commands include ``cmd`` (#1689 E1)."""
         return [s for s in segments if cmd in extract_commands(s)]
     ```

     Keep `get_command_for_validation` unchanged.
   - `hooks.py:24`: add `get_segments_for_validation` to the existing
     `from .parser import ...`, with names in alphabetical order. `ruff format`
     may wrap the line, which is fine.
   - `hooks.py:187-199` (`bash_security_hook`):

     ```python
     if cmd in VALIDATORS:
         validator = VALIDATORS[cmd]
         for cmd_segment in get_segments_for_validation(cmd, segments) or [command]:
             token = set_worktree_root(cwd)
             try:
                 allowed, reason = validator(cmd_segment)
             finally:
                 reset_worktree_root(token)
             if not allowed:
                 return {"decision": "block", "reason": reason}
     ```

   - `hooks.py:245-257` (`validate_command`): the same loop with
     `set_worktree_root(str(project_dir))`, returning `(False, reason)` on
     the first refusal.
   - If `get_command_for_validation` has no remaining callers, leave it in
     place anyway. Removing it is out of scope.

   Traps:
   - E1 applies to every validator (git, rm, chmod, ...). A command that
     used to pass, like `ls && rm -rf ../x`, is now refused. That is a
     stated risk.
   - Pipes are not split into segments, so `git status | git -c core.pager=x log`
     was already blocked. E1 covers `&&` and `;`.
   - `bash -c '...'` wrappers still bypass every validator. That is a
     follow-up against `parser.py`. Do not re-split `bash -c` here.

6. `CHANGELOG.md:1-3`, follow-ups and PR. Session model, not the coder. →
   verify by `python -m pytest $T -q` (expect **165 passed, 2 skipped**),
   then G, then confirm that
   `git diff main -- apps/web-server/server/services/build_backend.py` is
   empty.
   - CHANGELOG under `## [Unreleased]` → `### Security` (already at line 3):
     one entry covering the fsmonitor pin, `git config` write refusal,
     per-segment validation (E1), the E2 key families, `_git_env` scrubbed
     and pinned, and that project hooks no longer run in `WorktreeManager`.
   - If anything deviated from this plan, update `plan/` in the same commit
     as the code.
   - Manual checks:
     - the intent's PWNED repro under `child_env()` and under `_git_env()`:
       no `PWNED`
     - the k3d kubejob smoke test with `AIFACTORY_BASH_SANDBOX=false`: the
       base fetch succeeds and there is no `PWNED`
   - Open 4 follow-up issues:
     - ssh, pager and editor pins
     - `--no-ext-diff`/`--no-textconv` on server diff and `log -p` calls
     - a Write/Edit `.git` guard after #1672/#1673
     - `bash -c` unwrapping in `security/parser.py`
   - The PR links intent, spec and plan, says which steps the coder did,
     and states these risks:
     - direct writes (Write/Edit, redirects, scripts) stay open
     - `bash -c` wrappers bypass every validator
     - textconv and filter drivers stay open
     - project hooks are disabled in `WorktreeManager`
     - a non-GH `*_KEY`/`*_TOKEN` helper fails loudly at `worktree.py:420-428`
     - E1 may newly refuse agent commands
     - reads, unsets and values of dangerous keys are refused too
     - the `GIT_CONFIG` layout is now `COUNT=n+2`, so #1674/#1688/#1692
       must rebase
     - git below 2.36 runs `/bin/false` harmlessly

   Traps:
   - Expected rebase overlaps: #1671/#1673 (`tests/test_child_process_env.py`,
     and #1671 also `child_env.py`), and #1669/#1670 (`agent_kubejob.py`,
     not touched here, but it spawns through `child_env`, so `COUNT`
     changes). #1672 (`pr_endgame.py`) is unaffected at line 967. Any
     branch that adds `_BACKEND_FILES` entries can conflict.
   - Do not push or comment on GitHub until the user asks.

## Tests

Run from the worktree with the venv on PATH.

1. `python -m pytest $T -q`
   - Baseline at `d80f7b79`: 140 passed, 2 skipped.
   - Expected: **165 passed, 2 skipped**. Per file: child_process_env 17,
     no_unscrubbed_spawn 2, command_sandbox 64, worktree 25,
     worktree_concurrent_lock 9, trusted_plan 32, sandbox_escape_corpus 15
     (+2 skipped).
2. `python -m pytest tests -q`: no new failures against main.
3. Web server, from `apps/web-server`:
   `python -m pytest tests/test_control_plane_reads_the_pushed_work.py tests/test_merge_worktree_logger.py tests/test_no_worktree_head_as_branch.py tests/test_semantic_conflicts_read_the_pushed_work.py tests/test_worktree_branch_as_truth.py tests/test_worktree_tools_extraction.py tests/test_resolve_conflicts_reads_the_commit.py -q -o asyncio_mode=auto`.
   Expect 27 passed, the same as the baseline. Without
   `-o asyncio_mode=auto` the async tests fail.
4. Lint gate G (above), all clean.
5. `git diff main -- apps/web-server/server/services/build_backend.py` is
   empty.
6. Manual: the PWNED repro under `child_env()` and `_git_env()`, and the
   k3d kubejob smoke test (`AIFACTORY_BASH_SANDBOX=false`).

Each path has a mutation check. Every test below goes red when its code is
reverted:

| Path | Mutation | Red test |
|---|---|---|
| Q1 pin | drop fsmonitor (COUNT=n+1) | real-git test, both layout tests |
| Q1 order | fsmonitor written at KEY_n | both layout tests |
| Q4 | revert to the `os.environ` copy | `_git_env` test (`DATABASE_URL`, pins) |
| Q4 keep | drop `keep=GITHUB_KEEP` | `_git_env` test (`GH_TOKEN`) |
| Q4 ambient | drop the `_AMBIENT_GIT_VARS` filter | `_git_env` test (`GIT_DIR`) |
| Q2 `-C` | `-C` does not consume its argument | `git -C d config core.pager x` |
| Q2 naive | `"config" in tokens` | legit `git commit -m config` |
| Q2 edit | drop `-e`/`--rename-section` | `git config -e`, `--rename-section foo core` |
| Q2 unparseable | drop the new branch | `test_unparseable_git_config_fails_closed` |
| E1 hook | single segment again | `test_chained_git_segment_blocked_by_hook` |
| E1 validate_command | single segment again | `git status && git -c core.fsmonitor=x status` |
| E2 | drop `.program`/`credential.`/`include.path` | `gpg.program`, `credential.helper`, `include.path` |
| Q2 over-refusal | refuse every `git config` | the 6 legit cases |

## Rollback

- **Before merge:** drop the implementation commits on the task branch
  (`git reset --hard <plan-approval-sha>`). The intent, spec and plan
  commits stay.
- **After merge:** `git revert <impl-sha>` for each implementation commit
  (code, tests and CHANGELOG). If the autonomy matrix was regenerated in
  them, the revert restores it, so confirm with
  `python scripts/gen_autonomy_matrix.py --check`. Re-run Tests command 1
  and expect the baseline of 140 passed, 2 skipped.
- **Partial rollback**, if a false positive blocks agents in production:
  revert only `git_validators.py` and its `test_command_sandbox.py` cases
  (Q2/E2). The env pins (Q1/Q4) and E1 are independent of them and stay.
  If E1 over-refuses, revert `parser.py`/`hooks.py` and the hook test. If
  `_git_env` breaks fetch auth in the kubejob, revert only the `worktree.py`
  change, its `_git_env` test and the `_BACKEND_FILES` entry. The Q1 pin
  stays for every other `child_env` caller.
