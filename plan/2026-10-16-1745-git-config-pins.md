---
status: approved
issue: 1745
spec: spec/2026-10-16-1745-git-config-pins.md
---

# Plan: pin the remaining agent-writable git config keys

Worktree `<lane scratchpad>/AIFactory-1745b`, branch `fix/1745-git-config-pins`
off `origin/dev` fb97ccd6. All paths are repo-relative. Line numbers come from
fb97ccd6. Test env: `PATH=<main clone>/apps/backend/.venv/bin:$PATH`.

## Approved decisions (carried from the spec)

- D1: `child_env()` appends six more pins after the existing six:
  `core.askPass=` (empty), `credential.helper=` (empty, a reset),
  `credential.https://github.com.helper=!gh auth git-credential`,
  `sequence.editor=true`, `remote.origin.uploadpack=git-upload-pack`,
  `remote.origin.receivepack=git-receive-pack`. It also sets
  `env.setdefault("GIT_PAGER", "cat")`. The pins move into a module tuple and
  `pin_git_config(env, *, editor="true", pager="cat")`, which sets both
  editor keys and the pager key plus the `GIT_PAGER` default.
- D2: no change for `uploadpack.packObjectsHook` (git ignores it in repo
  config), `filter.*` / `merge.*.driver` (#1761), `diff.*` (#1718), or
  `url.*.insteadOf` (accepted).
- D3: `PTYSession.start()` applies `pin_git_config` last. The editor is
  `VISUAL`, else `EDITOR`, else `vi`. The pager is `PAGER`, else `less`. The
  terminal does not go through `child_env()`.
- D4: matrix section `B2. Child-process git config pins` with derived id
  `sandbox.git_config_pins`, a TOML control entry, a `--check` floor and the
  regenerated outputs.

## Steps

### Step 1: `pin_git_config` in `apps/backend/core/child_env.py`

- Above `def child_env` (line 35), add:
  ```python
  # (key, value) pins; None means the editor/pager argument (#1689, #1717, #1745).
  _GIT_PINS: tuple[tuple[str, str | None], ...] = (
      ("core.hooksPath", "/dev/null"),
      ("core.fsmonitor", "false"),
      ("protocol.ext.allow", "never"),
      ("core.sshCommand", "ssh"),
      ("core.pager", None),
      ("core.editor", None),
      ("core.askPass", ""),
      ("credential.helper", ""),  # empty resets every inherited helper
      ("credential.https://github.com.helper", "!gh auth git-credential"),
      ("sequence.editor", None),
      ("remote.origin.uploadpack", "git-upload-pack"),
      ("remote.origin.receivepack", "git-receive-pack"),
  )
  ```
  and `def pin_git_config(env: dict[str, str], *, editor: str = "true",
  pager: str = "cat") -> dict[str, str]`. It reads `n` from
  `GIT_CONFIG_COUNT`, using the same try/except as lines 63-66. It writes
  `GIT_CONFIG_KEY_{n+i}` / `VALUE` for each pin, using `pager` for
  `core.pager` and `editor` for the two editor keys. It sets
  `GIT_CONFIG_COUNT` and `env.setdefault("GIT_PAGER", pager)`, and returns
  `env` (mutated in place).
- Replace lines 63-79 with `return pin_git_config(env)`. Update the docstring
  (lines 41-52) to name the new pins and `GIT_PAGER`.
- `apps/web-server/server/utils/subprocess_env.py`: add
  `pin_git_config = _core.pin_git_config` next to `lfs_push_argv`, and add it
  to `__all__`.
- Trap: the order must keep the existing six at indices n..n+5, because tests
  and `github_env` rely on it. `github_env` appends after, reading the new
  count.
- Check: `python -c "import sys; sys.path.insert(0,'apps/backend'); from core.child_env import child_env; e=child_env(); print(e['GIT_CONFIG_COUNT'], e['GIT_PAGER'])"`
  prints `12 cat` (with no `GIT_CONFIG_COUNT`/`GIT_PAGER` in the shell).

### Step 2: tests for the server pins

- `tests/test_child_process_env.py`:
  - `:74` `"7"`→`"13"`; `:93` `"6"`→`"12"`; `:316` `"7"`→`"13"`. Add
    assertions for indices 6-11 (or 7-12 with an existing entry) in the two
    index tests at `:67-106`.
  - New `test_real_git_repo_exec_keys_pinned(secret_env, tmp_path)`. It runs
    `git init` on a temp repo and writes the config with `git config`:
    `core.askPass=<tmp>/A`, `credential.helper=<tmp>/C`,
    `sequence.editor=<tmp>/Q`, `remote.origin.uploadpack=<tmp>/U`,
    `remote.origin.receivepack=<tmp>/R`. Then it uses `env=child_env()` and
    asserts:
    - `git config --get core.askPass` → `""`
    - `--get-all credential.helper` last line `""`
    - `--get-all credential.https://github.com.helper` last line
      `!gh auth git-credential`
    - `--get sequence.editor` → `true`
    - `remote.origin.uploadpack` → `git-upload-pack`, `receivepack` →
      `git-receive-pack`
    - `env["GIT_PAGER"] == "cat"`

    It runs no planted program.
  - The fixture `secret_env` also does `monkeypatch.delenv("GIT_PAGER",
    raising=False)`.
- `tests/test_github_env.py:107-121`: indices 7 and 8 → 13 and 14, count
  `"9"`→`"15"`. Add asserts for keys 7-12.
- `git grep -n 'GIT_CONFIG_COUNT\|GIT_CONFIG_KEY_' -- tests apps/*/tests`:
  fix any other hard-coded index or count.
- Prove the regression: run the new test against the fb97ccd6
  `child_env.py`, copied in temporarily, and see it fail. Then restore.
- Check: `pytest -q tests/test_child_process_env.py tests/test_github_env.py tests/test_lfs_push.py tests/test_worktree_concurrent_lock.py tests/test_worktree_fresh_base.py tests/test_agent_cli_env.py tests/test_tool_executor_env.py`

### Step 3: web terminal

- `apps/web-server/server/pty/session.py`: add the import
  `from ..utils.subprocess_env import pin_git_config`. After lines 61-62
  (`if self.env: env.update(self.env)`), add:
  ```python
  # Agent worktrees share .git/config; pin git's exec keys, keep the user's editor/pager (#1745).
  pin_git_config(
      env,
      editor=env.get("VISUAL") or env.get("EDITOR") or "vi",
      pager=env.get("PAGER") or "less",
  )
  ```
- New `apps/web-server/tests/test_pty_git_pins.py`. It skips if
  `ptyprocess` is missing and monkeypatches
  `session.PtyProcessUnicode.spawn` to capture `env`. It sets `EDITOR=nano`
  and unsets `VISUAL`/`PAGER`/`GIT_PAGER`/`GIT_CONFIG_COUNT`. It starts
  `PTYSession(env={"CLAUDE_CODE_OAUTH_TOKEN": "t"})` and asserts:
  - `core.editor` and `sequence.editor` are `nano`
  - `core.pager` is `less` and `GIT_PAGER` is `less`
  - `core.hooksPath=/dev/null` and `credential.helper=""` are present
  - `CLAUDE_CODE_OAUTH_TOKEN == "t"`
- Trap: the import path. `session.py` is in the `server.pty` package, and
  `subprocess_env` inserts `apps/backend` on `sys.path` itself.
- Check: `cd apps/web-server && pytest -q tests/test_pty_git_pins.py`

### Step 4: autonomy matrix row

- `scripts/gen_autonomy_matrix.py`:
  - Add `_section_git_pins(tabs)` after `_section_wiring`. It imports
    `core.child_env as ce` (lazily, like `_section_wiring`). Under
    `_env(GIT_CONFIG_COUNT=None, GIT_PAGER=None)` it calls `ce.child_env()`
    and builds rows `[f"`{key}`", f"`{value}`"]` for i in
    range(count). It appends a row ``["`GIT_PAGER` (env default)", "`cat`"]``.
    The header is `["key", "pinned value"]` and the title
    `"## B2. Child-process git config pins"`. It returns `(lines, len(rows))`.
  - `_SECTIONS` (line ~857): add `("git_pins", _section_git_pins)` after
    `("wiring", ...)`.
  - `_derived_ids` (line 792): add `ids += ["sandbox.git_config_pins"]`.
  - `check()` floors (line ~933): add
    `"git_pins": (c["git_pins"] == len(ce._GIT_PINS) + 1)`. Use a
    module-level import of `core.child_env` guarded like the others, or import
    inside `check`. Add `git_pins={c['git_pins']}` to the ok print.
- `docs/compliance/control-objectives.toml`: add a `[controls."sandbox.git_config_pins"]`
  entry. Objective: "Every child process the server spawns, and the web
  terminal, runs git with command-scope pins that override the repo config
  keys an agent could use to run a program (hooks, fsmonitor, ext transport,
  ssh, pager, editor, askpass, credential helper, sequence editor, upload/receive
  pack)." Evidence: `apps/backend/core/child_env.py`. Frameworks:
  `["ISO 27001:2022 A.8.28", "SOC 2 CC6.8", "NIST 800-53 SI"]`.
- Run `python scripts/gen_autonomy_matrix.py`, then `--check`.
- Trap: `_env` must clear `GIT_PAGER` too, or a host `GIT_PAGER` changes the
  output.
- Check: `python scripts/gen_autonomy_matrix.py --check && pytest -q tests/test_gen_autonomy_matrix.py`

### Step 5: CHANGELOG

- Add a `### Security` bullet under `[Unreleased]`: the new pins and the
  terminal change. It notes the risk that host non-gh credential helpers no
  longer apply.

## Tests

The step 2 regression test (real git, temp repo) and the step 3 terminal env
test. The matrix `--check` also covers the pins. Ruff check and format on
every touched Python file.

## Rollback

Revert the step commits. The pins are env-only and nothing is persisted.

## Deviations

- Steps 1-2 (D1 changed): the `remote.origin.uploadpack` / `receivepack` pins
  are dropped. Any `remote.origin.*` key in command-scope config makes git
  report a phantom `origin` remote: `git remote` prints `origin` and
  `git remote -v` prints `origin<TAB>`, in a repo with no origin (git 2.55.0).
  That breaks `core/worktree.py:375` (local-only repos) and the
  "No git remote configured" check at `routes/git.py:1006`. Exec through a
  planted uploadpack also needs the agent to point origin at a local path,
  which is the same class as #1761. So `child_env()` gets 4 new pins
  (count 10, not 12), and step 2's indices and counts shift to match
  (13→11 with an existing entry, 12→10 without; `test_github_env` helper
  entries at 11/12, count 13). agy proposed changing `worktree.py` instead;
  rejected because it only fixes one of the callers.
- Step 2 was finished by the lane agent. agy's handoff returned an empty JSON
  response, so it counts as a failed handoff, and its diff was reviewed and
  reworked by hand.
- Step 4 was finished by the lane agent: agy's JSON response was empty again
  (a failed handoff). Its diff was kept. One addition: an empty pin value
  renders as `(empty)` (`_code`) instead of an empty backtick pair, which is
  not valid markdown or MDX.
- Review changes (lead review of #1811):
  - D3 changed: `PTYSession.start()` re-adds the user's own credential
    helpers after the reset. `_user_credential_helpers` reads
    `git config --system|--global --get-regexp '^credential\.(.+\.)?helper$'`
    (5 s timeout; a failure counts as none). That covers URL-scoped helpers
    like `glab auth setup-git`. Repo config is never read, so planted helpers
    stay reset. The spec's terminal credential risk no longer applies.
  - `session.py` uses an absolute import, and the comment is wrapped to 100
    columns. `tests/test_pty_git_pins.py` is ruff-clean against
    `standards/ruff.toml`. `session.py` is back at its 4 baseline findings.
  - The `child_env()` docstring now says the `credential.helper` reset wipes a
    helper a caller put in `GIT_CONFIG_*`/`extra` earlier.
    `test_reset_wipes_a_callers_earlier_credential_helper` pins that.
  - New `test_real_git_planted_helper_and_askpass_do_not_run`: real
    `git credential fill` for a fake host, PATH holding only git and touch
    (no gh), no network. Neither marker appears. Both new tests fail on the dev
    `child_env.py`.
  - New `test_pty_session_restores_the_users_global_credential_helpers`.
