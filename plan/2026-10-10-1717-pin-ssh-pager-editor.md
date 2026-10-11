---
status: approved
issue: 1717
spec: spec/2026-10-10-1717-pin-ssh-pager-editor.md
---

# Plan: pin core.sshCommand, core.pager and core.editor in child_env

Worktree `/mnt/code/Source-home/GitHub/AIFactory-1717`, branch
`fix/1717-pin-ssh-pager-editor`. All paths are repo-relative. Every line range
below was read from `origin/dev` 0bf19434. That is what the branch holds after
step 0.

## Approved decisions (self-contained summary)

**Problem in one line.** `apps/backend/core/child_env.py:child_env()` pins
`core.hooksPath`, `core.fsmonitor` and `protocol.ext.allow` through
`GIT_CONFIG_*`. An agent-written `.git/config` can still set `core.sshCommand`,
`core.pager` or `core.editor`, and git on the server then runs that command,
for example during `maybe_push_workspace_branch`.

**Base.** The worktree HEAD fd11397e sits on ef25678b, which is older than
#1689 and #1688. Its `child_env.py:55-61` has only the hooksPath pin. #1722
(#1689, ed266886) and #1726 (#1688, bf4fd672) are both on `origin/dev`
(0bf19434), so step 0 rebases onto it.

**Decisions.**

- **Q1.** Pin `core.sshCommand=ssh`, which is git's default. Do not refuse
  non-https origins, because ssh remotes stay supported
  (`apps/web-server/server/routes/projects.py:147-150`).
- **Q2.** Keep `GIT_SSH_COMMAND`, `GIT_PAGER` and `GIT_EDITOR` from the server
  env as the documented operator override. Do not strip them in `child_env`.
  Git ranks them above config.
- **Q3.** Also pin `core.pager=cat` and `core.editor=true` as defence in depth,
  rather than arguing they cannot be reached. `true` matches the `pr_endgame`
  precedent, and a commit without `-m` then aborts on an empty message.
- **Q4.** Do not fold this into #1689. Append the new pins after #1689's three.
  Do not wait for #1719.
- **Q5.** Scope is these three keys only. `protocol.ext.allow` is already
  pinned by #1689. A follow-up issue covers `credential.helper`,
  `core.askPass`, `sequence.editor`, `filter.*`, diff/merge drivers,
  `uploadpack`/`receivepack` and `url.*.insteadOf` (allow-list or
  `GIT_CONFIG_NOSYSTEM` shape). The same follow-up takes Risk 1
  (`.aifactory/.env` can inject `GIT_SSH_COMMAND` or `GIT_CONFIG_PARAMETERS`,
  `pr_review_service.py:271-274`) and the autonomy-matrix row.
- **Pin order and shape.** Three explicit pairs appended after
  `protocol.ext.allow`: `KEY_{n+3}=core.sshCommand`/`ssh`,
  `KEY_{n+4}=core.pager`/`cat`, `KEY_{n+5}=core.editor`/`true`, and
  `GIT_CONFIG_COUNT=str(n + 6)`. No `GIT_PINS` tuple or loop refactor, and
  #1689's lines are not rewritten.
- **Inherit, do not touch.** These all get the pins through `child_env()`, so
  none gets a call-site pin:
  - `workspace_fetch._git` and the push (`workspace_fetch.py:91-157`).
  - `authed_push_url`: the non-GitHub branch returns `child_env()` at
    `git_credentials.py:136-139`, and the GitHub branch spreads it at 151-157.
  - `github_env`, which reads `GIT_CONFIG_COUNT` and appends after the pins
    (`git_credentials.py:95-104`).
  - `worktree._git_env` (`worktree.py:92-110`).
  - `server/utils/subprocess_env.child_env` (58-68) and `make_subprocess_env`
    (71-88). The runners/github spawns inherit through
    `pr_review_service.py:255`.
- **No autonomy-matrix row.** `gen_autonomy_matrix.py` cannot express one
  without a generator change, so it goes to the follow-up. This deviation from
  proposed answer 6 was approved with the spec.
- **Accepted risks, not fixed here.**
  - An operator's global or system `core.sshCommand`, pager or editor is
    overridden, and so is legacy `GIT_SSH`.
  - The agent runner inherits `pager=cat` and `editor=true`.
  - A command-line `-c` still beats the pins. `pr_endgame.py:1024-1025` relies
    on this with `-c core.editor=true`.

**Deviations from the spec (recorded here).**

- **D1, required.** `tests/test_github_env.py:102-117` hard-codes the #1689
  layout: `KEY_4`/`KEY_5` are the helper entries and `COUNT == "6"`. With
  three more pins, the helpers move to `KEY_7`/`KEY_8` and COUNT becomes
  `"9"`. The spec's file list names only `tests/test_child_process_env.py`.
  This file is added to the same commit. `tests/test_worktree_concurrent_lock.py:252-257`
  builds a dict over the pins, so it is unaffected.
- **D2, left for the approver.** The `worktree._git_env` docstring
  (`worktree.py:93-103`) mentions only the hooksPath and fsmonitor pins, and
  it was already stale after #1689. It is out of the spec's "no other code
  changes" and stays untouched unless the approver asks.

**Who does what.** Steps 1-3 edit 4 files, so by policy they go to one `coder`
agent (Sonnet). The session model does step 0 and the commit. A fresh Opus
agent then reviews, given only this plan path and `git diff`.

## Steps

0. **Branch: rebase onto `origin/dev`** (session model, not the coder).
   Run `git -C /mnt/code/Source-home/GitHub/AIFactory-1717 rebase origin/dev`.
   The branch holds only intent, spec and plan commits, so no conflicts are
   expected.
   → verify by `git show HEAD:apps/backend/core/child_env.py | sed -n 64,70p`,
   which shows the `protocol.ext.allow` pin and `str(n + 3)`.
   Traps: the coder cannot run checkout, stash or restore, so do this before
   the handoff.

1. **Tests first (red).**
   - `tests/test_child_process_env.py:67-82`
     (`test_hooks_path_appended_after_existing_entry`): change COUNT on line
     74 from `"4"` to `"7"`. Add `KEY_4`/`VALUE_4 == core.sshCommand`/`ssh`,
     `KEY_5`/`VALUE_5 == core.pager`/`cat` and
     `KEY_6`/`VALUE_6 == core.editor`/`true`. Keep the existing credential
     helper at index 0.
   - `tests/test_child_process_env.py:85-93`
     (`test_hooks_path_is_entry_zero_without_count`): change COUNT on line 87
     from `"3"` to `"6"`. Add `KEY_3`/`VALUE_3 == core.sshCommand`/`ssh`,
     `KEY_4`/`VALUE_4 == core.pager`/`cat` and
     `KEY_5`/`VALUE_5 == core.editor`/`true`.
   - `tests/test_child_process_env.py:210-227`
     (`test_core_child_env_extra_count_appends_hooks_entry`): change COUNT on
     line 222 from `"4"` to `"7"`. Add `KEY_4 == core.sshCommand`,
     `KEY_5 == core.pager` and `KEY_6 == core.editor`.
   - `tests/test_github_env.py:102-117`
     (`test_helper_entries_follow_hooks_path_and_existing_entry`, deviation
     D1): add `KEY_4`/`KEY_5`/`KEY_6 == core.sshCommand`/`core.pager`/`core.editor`.
     Move the helper asserts from `KEY_4`/`KEY_5` and `VALUE_4`/`VALUE_5` to
     `KEY_7`/`KEY_8` and `VALUE_7 == ""`/`VALUE_8 == HELPER_VALUE`. Change
     COUNT from `"6"` to `"9"`.
   - New `test_real_git_ssh_pager_editor_pinned` in
     `tests/test_child_process_env.py`, inserted after
     `test_real_git_hook_disabled_by_child_env` (166-180). The pattern comes
     from the fsmonitor real-git test (96-149), including its ambient `GIT_*`
     delenv at 99-107. The file's noqa header (S603/S607/PLW1510) covers it.
     Reference body:

     ```python
     def test_real_git_ssh_pager_editor_pinned(
         secret_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
     ) -> None:
         """An agent-written .git/config cannot pick ssh, pager or editor (#1717)."""
         from core import workspace_fetch

         for k in (
             "GIT_SSH_COMMAND", "GIT_SSH", "GIT_PAGER", "PAGER", "GIT_EDITOR",
             "VISUAL", "EDITOR", "GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE",
             "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY",
         ):
             monkeypatch.delenv(k, raising=False)
         monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
         monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
         monkeypatch.setenv("WORKSPACE_URI", "s3://dummy/ws.tar")
         spec = "001-pin"
         wt = tmp_path / ".aifactory" / "worktrees" / "tasks" / spec
         marker = tmp_path / "S"
         pager = f"touch {tmp_path}/P; cat"
         editor = f"touch {tmp_path}/E; true"

         def git(*args: str, env: dict[str, str] | None = None) -> str:
             return subprocess.run(
                 ["git", "-C", str(wt), *args],
                 env=env or {**os.environ},
                 capture_output=True, text=True, timeout=30,
             ).stdout.strip()

         subprocess.run(["git", "init", "-q", "-b", "main", str(wt)],
                        env={**os.environ}, check=True, timeout=30)
         git("-c", "user.name=t", "-c", "user.email=t@t",
             "commit", "-q", "--allow-empty", "-m", "i")
         git("remote", "add", "origin", "ssh://git@127.0.0.1:1/r.git")
         git("config", "ssh.variant", "ssh")  # 'simple' variant rejects the port
         git("config", "core.sshCommand", f"touch {marker}; false")
         git("config", "core.pager", pager)
         git("config", "core.editor", editor)
         git("push", "origin", "HEAD:main")  # control: unpinned git runs it
         assert marker.exists()
         marker.unlink()
         assert workspace_fetch.maybe_push_workspace_branch(tmp_path, spec) is False
         assert not marker.exists()
         pinned = child_env()
         assert git("var", "GIT_PAGER") == pager
         assert git("var", "GIT_EDITOR") == editor
         assert git("var", "GIT_PAGER", env=pinned) == "cat"
         assert git("var", "GIT_EDITOR", env=pinned) == "true"
         monkeypatch.setenv("GIT_PAGER", "less")
         monkeypatch.setenv("GIT_EDITOR", "vi")
         monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -i k")
         op = child_env()
         assert op["GIT_SSH_COMMAND"] == "ssh -i k"
         assert git("var", "GIT_PAGER", env=op) == "less"
         assert git("var", "GIT_EDITOR", env=op) == "vi"
     ```

     Notes on the test:
     - It plants inline shell commands instead of the spec's "marker-writing
       scripts". The effect is the same, and no extra files are needed.
     - `maybe_push` hits the non-GitHub branch of `authed_push_url` (ssh
       URL), so it uses core `child_env()` directly. `pinned` and `op` use
       the web-server wrapper, so both entry points are covered.

   → verify by `pytest -o asyncio_mode=auto tests/test_child_process_env.py tests/test_github_env.py -q`.
   The 4 edited tests and the new real-git test fail, and nothing else does.

   Traps:
   - Every `subprocess.run` needs `env=` (`tests/test_no_unscrubbed_spawn.py`)
     and `timeout=30`.
   - pre-commit runs pytest inside a git hook, so the `GIT_DIR` family must be
     deleted (#819).
   - Claude Code sessions export `GIT_EDITOR=true`, and this machine sets
     `PAGER=less` and `EDITOR`/`VISUAL=nvim`. Without the delenv calls, the
     `GIT_EDITOR` asserts would pass for the wrong reason.
   - The `secret_env` fixture (37-39) only removes `GIT_CONFIG_COUNT`, `KEY_0`
     and `VALUE_0`.
   - `ssh.variant=ssh` is required. On git 2.55 the `simple` variant rejects
     the port, and the positive control then fails.
   - The internal push in `maybe_push_workspace_branch` has `timeout=180`,
     which the test cannot set. Port 1 refuses at once (about 10ms), so this
     is fine. If the runner has no `ssh` binary, git fails fast and the
     no-marker assert still holds, because the positive control proved the
     planted command would have run.
   - Root `tests/` is outside the ratchet's scope, but default ruff applies.
     The reference body above is not `ruff format` clean (packed tuple and
     call arguments), so `ruff format --check` fails on it as pasted. Run
     `ruff format tests/test_child_process_env.py` after inserting it, then
     `ruff format --check tests` must be clean. `ruff check` passes as is.

2. **Code (green).**
   - `apps/backend/core/child_env.py:64-70`: after line 69
     (`GIT_CONFIG_VALUE_{n + 2} = "never"`), add three explicit pairs:
     - `env[f"GIT_CONFIG_KEY_{n + 3}"] = "core.sshCommand"` and
       `env[f"GIT_CONFIG_VALUE_{n + 3}"] = "ssh"`
     - `env[f"GIT_CONFIG_KEY_{n + 4}"] = "core.pager"` and
       `env[f"GIT_CONFIG_VALUE_{n + 4}"] = "cat"`
     - `env[f"GIT_CONFIG_KEY_{n + 5}"] = "core.editor"` and
       `env[f"GIT_CONFIG_VALUE_{n + 5}"] = "true"`

     Change line 70 from `str(n + 3)` to `str(n + 6)`.
   - `apps/backend/core/child_env.py:45-47` (docstring): after "disables git
     hooks, fsmonitor and the ext:: transport via git env config", add one
     sentence. The pins also set the ssh command (`ssh`), pager (`cat`) and
     editor (`true`). `GIT_SSH_COMMAND`, `GIT_PAGER` or `GIT_EDITOR` in the
     server env overrides them.

   → verify by
   `pytest -o asyncio_mode=auto tests/test_child_process_env.py tests/test_github_env.py tests/test_no_unscrubbed_spawn.py tests/test_worktree_concurrent_lock.py -q`.
   This is green with 49 passed. Then run the mutation checks M1-M6 under
   Tests.

   Traps:
   - Do not rewrite #1689's lines, and do not add a loop or tuple.
   - Add no call-site pins. `workspace_fetch`, `git_credentials`,
     `worktree._git_env`, `subprocess_env` and `pr_review_service` inherit the
     pins.
   - Leave `worktree.py:93-103` alone (D2).
   - Mutations are applied and reverted by editing the file. The coder cannot
     use git restore or stash.

3. **CHANGELOG, follow-up issue and PR.**
   - `CHANGELOG.md:1-25`: under `## [Unreleased]` / `### Security`, next to
     the #1689 entry (5-15) and the #1688 entry (16-25), add one #1717
     bullet. It says `child_env()` also pins `core.sshCommand=ssh`,
     `core.pager=cat` and `core.editor=true`, so an agent-written
     `.git/config` cannot choose the server's ssh command, pager or editor.
     The operator override is `GIT_SSH_COMMAND`/`GIT_PAGER`/`GIT_EDITOR`.
   - Draft the follow-up issue with the Q5 scope, Risk 1 and the
     autonomy-matrix row.
   - Commit `fix(security): child_env pins core.sshCommand, core.pager and core.editor (#1717)`.
     The scope has no `#`. If implementation deviated, update this plan in the
     same commit.
   - Open the PR to `dev`. It links intent, spec and plan, and says which
     steps the coder did.

   → verify by `python scripts/gen_autonomy_matrix.py --check` (green; needs
   `yaml`) and `git diff origin/dev --stat`, which lists only the 4 files plus
   the `intent/`, `spec/` and `plan/` files for this slug.

   Traps:
   - #1692, #1631, #1632 and #1633 also touch `CHANGELOG.md`. Let them merge
     first, rebase #1717 last, and keep every entry, including #1689 and
     #1688.
   - #1633 (PR #1735) changes `workspace_fetch.py`, but only
     `maybe_fetch_usage`, not `maybe_push_workspace_branch`. No conflict.
   - `tests/test_security.py`'s `GitCommitValidator` fails while files are
     staged. This is environmental.
   - CodeQL: the test logs nothing, and the existing `maybe_push` warning on
     `res.stderr` is not touched.
   - Put the venv on PATH (`apps/backend/.venv/bin`) so the pre-commit hook
     runs.

## Tests

Run these from the rebased worktree with the project venv. That venv has
pytest-asyncio; Nix's system pytest lacks it, and there
`test_login_route_spawns_nothing_when_token_set` fails, which is environmental.

1. `pytest -o asyncio_mode=auto tests/test_child_process_env.py tests/test_github_env.py tests/test_no_unscrubbed_spawn.py tests/test_worktree_concurrent_lock.py`
   gives **49 passed** (21 + 17 + 2 + 9). The dev baseline is 48.
2. `pytest tests/test_child_process_env.py -k real_git_ssh_pager_editor` gives
   1 passed.
3. Mutation checks. Apply each one alone to `apps/backend/core/child_env.py`,
   rerun command 2, expect 1 failed at the listed assert, then edit it back.

   | # | Mutation | Must fail at |
   |---|---|---|
   | M1 | `KEY_{n+3}` `"core.sshCommand"` → `"aifactory.mutant"` | `assert not marker.exists()` |
   | M2 | `KEY_{n+4}` → `"aifactory.mutant"` | `git var GIT_PAGER` under `pinned == "cat"` |
   | M3 | `KEY_{n+5}` → `"aifactory.mutant"` | `git var GIT_EDITOR` under `pinned == "true"` |
   | M4 | COUNT back to `str(n + 3)` | `assert not marker.exists()` |
   | M5 | add `env["GIT_PAGER"] = "cat"` before `return env` | `== "less"` only |
   | M6 | add `env.pop("GIT_SSH_COMMAND", None)` before `return env` | `op["GIT_SSH_COMMAND"] == "ssh -i k"` only |

   M1 and M4 also turn the unit index tests red in command 1. After the last
   mutation, `git diff apps/backend/core/child_env.py` shows only the intended
   change.
4. `ruff format --check apps/backend apps/web-server scripts tests` and
   `ruff check apps/backend apps/web-server scripts tests` are clean.
5. With the changes staged:
   - `python scripts/cq_ratchet.py --staged --ruff "$(command -v ruff)" --config standards/ruff.toml --paths 'apps/backend/*.py' 'apps/web-server/*.py' 'scripts/*.py'`
   - the same with `--tool mypy --mypy "$(command -v mypy)" --config standards/mypy.ini`

   Both pass. `child_env.py` is the only file in ratchet scope.
6. `python scripts/gen_autonomy_matrix.py --check` is green. No matrix source
   cites `child_env.py`, `workspace_fetch.py` or `git_credentials.py`.

## Rollback

The change is configuration inside one function. `git revert <commit>` (or a
revert of the PR) removes the three pins, their test asserts and the CHANGELOG
bullet together. Afterwards, Tests command 1 gives 48 passed.

Operator workaround without a deploy: set `GIT_SSH_COMMAND`, `GIT_PAGER` or
`GIT_EDITOR` in the server environment. Each one overrides its pin, which M5
and M6 prove.

## Approver decisions (2026-10-11)

- D2: leave the stale `worktree._git_env` docstring (worktree.py:93-103) unchanged in this PR.
