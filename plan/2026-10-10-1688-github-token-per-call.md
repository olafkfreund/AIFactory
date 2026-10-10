---
status: approved
issue: 1688
spec: spec/2026-10-10-1688-github-token-per-call.md
---

# Plan: pass the GitHub token per call, not in the child env

Line numbers refer to this checkout at `c418ee92` (the spec-approve commit).
Where the spec's ranges differ from the checkout, this plan uses the checkout
(see "Drift from the spec" at the end).

## Approved decisions (self-contained summary)

1. **Mechanism (D1).** For each call that needs GitHub auth, the server
   writes the token to `hosts.yml` (mode 0600, `O_EXCL`) in a fresh
   `tempfile.mkdtemp(prefix="aif-gh-")` dir (0700) and sets
   `GH_CONFIG_DIR` to that dir. The file is JSON (valid YAML):
   `{"github.com": {"oauth_token": <token>, "user": "x-access-token",
   "git_protocol": "https"}}`. Use `USERNAME` for the user value. `gh` reads
   the file directly. `git` gets the token through `gh auth git-credential`,
   scoped to `https://github.com`. The dir is removed in `finally`. This
   narrows the exposure window but does not close it: the file can be read
   through `/proc/<child>` while the call runs (Risk 1).
2. **`github_env(base_env)` (D2).** A `@contextlib.contextmanager` returning
   `Iterator[dict[str, str]]`, in `apps/backend/core/git_credentials.py`
   next to `github_token()`.
   - It calls `github_token()` exactly once on entry. This is the hook point
     #1671 will change later.
   - **No token:** it yields `base_env` itself, unchanged, and creates
     nothing (D10).
   - **Token:** it yields a copy of `base_env` without
     `GITHUB_TOKEN`/`GH_TOKEN`/`GIT_PASS`, plus `GH_CONFIG_DIR=<dir>`. At
     index `n = GIT_CONFIG_COUNT` it adds two entries: key
     `credential.https://github.com.helper` with value `""`, which resets
     helpers from config files. Then the same key with value
     `!gh auth git-credential`. The count becomes `n + 2`. Parse `n` the
     same way as `core/child_env.py:51-54`: a non-int becomes 0, a negative
     becomes 0, and parsing never raises.
   - **Fail closed:** if any yielded env value contains the token, it raises
     `RuntimeError` (not `assert`), and the dir is still removed.
     `OSError` from `mkdtemp` or the write propagates. There is never a
     fallback to the env token.
3. **`sweep_github_dirs()` (D3)** in the same module. It removes every
   `aif-gh-*` entry in `tempfile.gettempdir()` with errors ignored. It
   never follows a symlink: it skips symlinks and does not unlink them, and
   calls `shutil.rmtree(ignore_errors=True)` only on real dirs. Call it
   once in `main.py` `lifespan`, right after `_make_non_dumpable()`.
4. **Threat bar (D4).** Narrow the window now. Full closure (PID isolation
   or a separate agent uid) is follow-up 2. A sandbox PID namespace is
   rejected for now, because the chart rules it out for unprivileged pods.
5. **Scope (D5, D16).** Only web-server children are in scope: the 18
   `child_env(keep=GITHUB_KEEP)` sites, plus `GIT_PASS` in
   `project_workspace_service`'s askpass. These stay untouched:
   - `core.child_env.child_env()`, `GITHUB_KEEP` (`RUNNER_KEEP` uses it)
   - `make_subprocess_env`, `RUNNER_KEEP` (`utils/subprocess_env.py:32-39`)
   - `core/git_credentials.authed_push_url` (58-99, `GIT_PASS` at :94)
   - `core/worktree.py:416-432`
   - the token that `gh auth login --web` stores in the default
     `GH_CONFIG_DIR`

   All of these go to follow-up 1.
6. **Remove `gh auth setup-git` (D6)** at the three web-server sites:
   `routes/pr.py`, `services/pr_endgame.py` and
   `services/completion_orchestration.py`. `core/worktree.py:421` stays.
   The per-call helper reset keeps it from mattering in server children.
7. **No waiting (D7, D8).** Do not wait for #1692 or #1671, and do not
   include #1671. Do not touch `make_subprocess_env` or `RUNNER_KEEP`.
8. **`gh auth login --web` (D9)** in `routes/github.py`.
   - Spawn it with `child_env()` and no keep.
   - Add an early guard: if `github_token()` is set, return success with
     "already authenticated via GITHUB_TOKEN" and start no child.
   - No temp dir: it is a long-running background process.
9. **Call-site pattern (D11).** `env=child_env(keep=GITHUB_KEEP)` becomes
   `with github_env(child_env()) as env:` wrapped around the spawn, using
   `env=env`.
   - The web-server `child_env` wrapper keeps adding `TRACEPARENT`.
   - Every git call at these sites is wrapped, local-only ones included.
     There is no network/local classification.
   - Special cases:
     - `build_backend._git` (D12):
       `github_env(child_env(extra={"GIT_TERMINAL_PROMPT": "0"}))`.
     - `pr_endgame` (D13): wrap once, inside `_default_runner`.
     - `project_workspace_service._run_git` (D14, async): the `with` covers
       `create_subprocess_exec`, `communicate()` and the timeout `kill()`.
10. **Workspace askpass (D15).** `_git_askpass_env` (any host, credential
    from the `git_credentials` table):
    - One `mkdtemp(prefix="aif-gh-")` dir holds the askpass script (0700)
      and a `pass` file (0600, `O_EXCL`).
    - The script line becomes `*) cat "$GIT_PASS_FILE" ;;`.
    - It yields `GIT_ASKPASS`, `GIT_TERMINAL_PROMPT=0`, `GIT_USER` and
      `GIT_PASS_FILE`. It never yields `GIT_PASS`.
    - `rmtree` runs in `finally`. The startup sweep covers this dir through
      the shared prefix.
11. **Risk 8, accepted (D17).** In `_run_git`, a github.com workspace
    credential gets both askpass and the gh helper. The server token wins,
    because git asks helpers before `GIT_ASKPASS`. Do not "fix" this.
12. **Rejected (D18):**
    - `GH_TOKEN` for gh plus askpass for git
    - one shared hosts.yml written at startup
    - pipe, fd, memfd or `O_TMPFILE`
    - the credential-cache daemon
    - keeping setup-git
    - a flag on `child_env`
    - try/finally at each site
    - a pid in the dir name
    - atexit or a reaper
    - rewriting `authed_push_url`/`worktree.py` now
    - a PID namespace now
13. **Follow-ups to open (D19):**
    1. Scope: `RUNNER_KEEP`, `worktree.py:416-432`, the `authed_push_url`
       `GIT_PASS`, and the token stored by the gh web login.
    2. Close the window: PID isolation or a separate agent uid.
14. **Imports.** The web server reaches `core` only through the `sys.path`
    insert in `server/utils/subprocess_env.py:22-24`.
    - `subprocess_env` therefore re-exports `github_env`,
      `sweep_github_dirs` and `github_token` and lists them in `__all__`
      (mypy strict needs `__all__` for a re-export).
    - Server files import them from there with one plain line, never
      `from core.git_credentials import ...`. Ruff isort would sort that line
      above the `sys.path` insert, and the import would fail at load.
    - `main.py` lazy-imports `sweep_github_dirs` inside `lifespan` with
      `from .utils.subprocess_env import sweep_github_dirs  # noqa: PLC0415`
      (main.py uses relative imports). It does not import from `core`, so the
      re-export has a user and the rule above has no exception.
15. **Handoff.** There are 4 code steps across 13+ files, so the `coder`
    agent (Sonnet) implements per the model split. A fresh Opus agent reviews
    the result against this plan and `git diff`.

## Steps

Shared verify prefix, for every command below:

```
export PATH=/mnt/code/Source-home/GitHub/AIFactory/apps/backend/.venv/bin:$PATH
cd /mnt/code/Source-home/GitHub/AIFactory-1688
```

### 1. Red tests

Files:

- new `tests/test_github_env.py`
- `apps/web-server/tests/test_pr_endgame.py:92-93`, plus one new test
- `apps/web-server/tests/test_merger.py:313`
- `tests/test_create_pr_fetches_branch.py:118-121`
- `apps/web-server/tests/test_workspace_argv_never_logged.py:41, 81-86, 258-262`
- `tests/test_git_credentials.py:7, 80-85, 104-106`

Change: write the cases listed under "Tests" below (the new file, A-O, and
the edits to existing tests).

Verify (expect red):

- `python -m pytest tests/test_github_env.py -q` fails. Collection fails on
  ImportError of `github_env`/`sweep_github_dirs`. With those stubbed, M
  reports 23 hits (18 keep, 3 setup-git, 2 `GIT_PASS"`).
- `python -m pytest apps/web-server/tests/test_pr_endgame.py apps/web-server/tests/test_workspace_argv_never_logged.py -q -o asyncio_mode=auto`
  fails.

Traps:

- `tests/test_git_argv_credential_backend.py` and
  `tests/test_child_process_env.py` must stay byte-identical. #1671 and
  #1673 edit the latter.
- The spawn fakes must read `GIT_PASS_FILE` and `hosts.yml` **at spawn
  time**. The dir is gone once the call returns.
- Read files through a sync helper (like `read_cmdline`), not `pathlib` or
  `open` inside `async def`. The repo `ruff.toml` does not select `ASYNC`,
  but `standards/ruff.toml` does, and the ratchet's `'apps/web-server/*.py'`
  glob (fnmatch, so `*` crosses `/`) covers `apps/web-server/tests`.
- The autouse fixture must patch `tempfile.tempdir` to `tmp_path`.
  Otherwise the sweep tests delete a running dev server's real
  `/tmp/aif-gh-*` dirs.
- Mark the token constant `# gitleaks:allow`.
- Run `ruff format` on the new file.
- If a pre-commit hook runs these tests, commit step 1 together with step 2.

### 2. Core helper and re-export

Files:

- `apps/backend/core/git_credentials.py:26-31` (imports), plus new code
  inserted after `github_token()` at 52-54
- `apps/web-server/server/utils/subprocess_env.py:26-30`
- `apps/web-server/server/main.py:104`

Changes:

- `git_credentials.py`:
  - Add `import json` and `import shutil`. `contextlib`, `os`, `stat`,
    `tempfile` and `Iterator` are already imported.
  - Add `github_env(base_env)` per decision 2: `tempfile.mkdtemp(prefix="aif-gh-")`.
    `mkdtemp` already creates the dir 0700, so add no `chmod`. Call it as
    `tempfile.mkdtemp` (module attribute), so test O can patch it.
  - Write `hosts.yml` with
    `os.open(path, O_WRONLY|O_CREAT|O_EXCL, 0o600)` and `json.dumps`.
  - Copy, strip, set `GH_CONFIG_DIR`, append the two helper entries, check
    the token, then `yield`. `shutil.rmtree(d, ignore_errors=True)` runs in
    `finally`.
  - Add `sweep_github_dirs()` per decision 3.
  - `authed_push_url` (58-99) stays as it is.
- `subprocess_env.py`:
  - Add
    `from core.git_credentials import github_env, github_token, sweep_github_dirs  # noqa: E402`
    on the line **after** the existing `from core import child_env as _core`
    line. Both ruff configs sort it there. Do not use
    `from core import git_credentials as _gc`: next to the aliased `_core`
    import, `standards/ruff.toml` (`combine-as-imports = true`) and the repo
    `ruff.toml` (default `false`) disagree, and one of them reports I001.
  - Add the three names to `__all__`.
  - `GITHUB_KEEP`, `RUNNER_KEEP` and `child_env` stay unchanged.
- `main.py`: right after `_make_non_dumpable()` at :104, lazy-import
  `sweep_github_dirs` from `.utils.subprocess_env` (decision 14; the
  `# noqa: PLC0415` pattern of :766) and call it.

Verify:

- `python -m pytest tests/test_github_env.py tests/test_git_credentials.py tests/test_no_unscrubbed_spawn.py -q`
  - A-K pass, and H/I pass or skip.
  - L, M, N, O and the `test_git_credentials` askpass guard stay red until
    steps 3-4.

Traps:

- `core/git_credentials.py` is in `_BACKEND_FILES` of
  `tests/test_no_unscrubbed_spawn.py`. Build the env from `base_env` only,
  never `os.environ` or `dict(os.environ)`.
- Fail closed with `raise RuntimeError`, never `assert`. Do not catch
  `OSError`.
- `standards/ruff.toml` selects `PTH`, and the ratchet lints this file with
  it. Build paths with `Path` (`Path(d) / "hosts.yml"`,
  `Path(tempfile.gettempdir()).glob("aif-gh-*")`, `p.is_symlink()`,
  `p.is_dir()`), not `os.path.join`, `os.chmod` or `os.listdir`. `os.open`
  with `O_EXCL` has no PTH rule and stays.
- When there is no token, return `base_env` itself (test G checks
  `env is base`).
- mypy strict (`standards/mypy.ini`) needs the typed
  `Iterator[dict[str, str]]` return.
- Run the cq_ratchet ruff and mypy commands from "Tests" on the staged files.
- #1671 also edits `subprocess_env.py` lines 26-40. Expect a conflict on
  `__all__`.

### 3. Mechanical call sites and setup-git removal

10 files, 17 of the 18 `keep=GITHUB_KEEP` sites: 14 wrapped (the table
below), the login, and the two setup-git calls that are deleted
(`routes/pr.py:291`, `completion_orchestration.py:402`). The 18th,
`project_workspace_service.py:468`, is step 4.

Imports. In the existing `from server.utils.subprocess_env import ...`
line, drop `GITHUB_KEEP` and add `github_env`, written as
`import child_env, github_env` (that order, see Traps):

- `routes/pr.py:38`
- `routes/git.py:23`
- `services/gh.py:17`
- `services/task_branch.py:32`
- `services/pr_data_service.py:23`
- `services/copilot_dispatch_service.py:30`
- `services/build_backend.py:109`
- `services/pr_endgame.py:35`
- `services/completion_orchestration.py:35`

In `routes/github.py:28`, the line becomes
`from server.utils.subprocess_env import child_env, github_token`.

Wrap each site in `with github_env(child_env()) as env:` and pass
`env=env` (paths under `apps/web-server/server/`):

| File | Site |
|---|---|
| `routes/pr.py` | 201-210 (env 207), git fetch base in `create_pr_from_task`; 304-312 (env 311), fetch `branch:branch`; 335-343 (env 342), push |
| `routes/git.py` | 56-64 (env 63), `run_git_command`; 1444-1452 (env 1451), `run_gh_command` (def 1433) |
| `services/gh.py` | 33-40 (env 39), **inside** the existing `try`, so `OSError` lands in `except OSError` at :48 |
| `services/task_branch.py` | 44-51 (env 50), inside the existing `try` |
| `services/pr_data_service.py` | 62-69 (env 68), `_run_gh` |
| `services/copilot_dispatch_service.py` | 65-79 (env 78), 114-127 (env 126), 146-158 (env 157) |
| `services/build_backend.py` | 949-961 (env 953): `with github_env(child_env(extra={"GIT_TERMINAL_PROMPT": "0"})) as env:` |
| `services/pr_endgame.py` | 67-76 (env 74): once, inside `_default_runner` |
| `services/completion_orchestration.py` | 404-411 (env 410): the `push = _sp.run(["git","push","origin","HEAD"], ...)` |

Delete setup-git:

- `routes/pr.py:279-292`: the #540 comment and the `gh auth setup-git`
  `subprocess.run` block.
- `services/pr_endgame.py:554-558`, in `create_pr`: the comment and
  `runner(["gh", "auth", "setup-git"], None)`.
- `services/completion_orchestration.py:398-403`: the
  `_sp.run(["gh","auth","setup-git"], ...)` call.

Login, in `routes/github.py` `start_github_auth` (def 599):

- Insert a guard between 612 and 614:
  `if github_token(): return {"success": True, "data": {..., "message": "already authenticated via GITHUB_TOKEN"}}`.
  Match the existing return shape and spawn nothing.
- At 635, `env=child_env(keep=GITHUB_KEEP)` becomes `env=child_env()`.
  Do not wrap it in `github_env`.

Verify:

- `python -m pytest apps/web-server/tests -q -o asyncio_mode=auto`
- `python -m pytest tests -q -m "not slow"`
- `grep -rn 'keep=GITHUB_KEEP\|"setup-git"' apps/web-server/server` prints
  nothing.
- `python scripts/gen_autonomy_matrix.py --check`

Traps:

- Import `github_token` from `server.utils.subprocess_env`, never from
  `core.git_credentials` (decision 14).
- Use plain names with no aliases. Keep any `noqa` on a single-line import,
  because the default and `standards/ruff.toml` configs disagree on sorting.
- Name order inside the import: a plain swap of `GITHUB_KEEP` for
  `github_env` gives `github_env, child_env`, which both configs report as
  I001 (isort's alphabetical order once the constant is gone). Write
  `child_env, github_env`.
- `completion_orchestration` uses the `_sp` module alias. Keep it. The push
  is deeply indented, so run `ruff format` before the ratchet.
- `routes/git.py:1433` duplicates `services/gh.run_gh_command`. Wrap both
  and do not dedupe them.
- Line shifts in `pr_endgame.py` change the autonomy matrix import closure.
  If `--check` fails, run `python scripts/gen_autonomy_matrix.py` and commit
  the outputs. The `autonomy-matrix.yml` workflow has no paths filter.
- Expect rebase conflicts with #1672 (`pr_endgame.py` `_default_runner` and
  `create_pr`) and #1671 (imports).

### 4. `project_workspace_service.py` (askpass and `_run_git`)

All lines are in `apps/web-server/server/services/project_workspace_service.py`:

- :45: the line becomes
  `from server.utils.subprocess_env import child_env, github_env`. Add
  `import shutil` between `import re` and `import stat`.
- 137-147: update the comment (it says values come from
  `GIT_USER`/`GIT_PASS`). Script line 145 becomes
  `*) cat "$GIT_PASS_FILE" ;;`.
- 150-176, `_git_askpass_env`:
  - Replace the `NamedTemporaryFile` with `mkdtemp(prefix="aif-gh-")`
    (0700) holding the script (0700) and `pass` (0600,
    `os.open(... O_EXCL, 0o600)`).
  - Yield `GIT_ASKPASS`, `GIT_TERMINAL_PROMPT=0`, `GIT_USER` and
    `GIT_PASS_FILE`, never `GIT_PASS`.
  - `shutil.rmtree` runs in `finally`. Update the docstring.
- :186: docstring of `_restore_sanitized_origin_best_effort`. "rides in
  GIT_PASS" becomes `GIT_PASS_FILE` (text only).
- 467-489, `_run_git` (def 438): the env build at 467-470 becomes
  `with github_env(child_env(extra={"GIT_ALLOW_PROTOCOL": "https:ssh:git", **(extra_env or {})})) as env:`.
  The block spans `create_subprocess_exec` (471-479) and
  `communicate()`/timeout `kill()` (481-489). The returncode check may stay
  inside the block or follow it.

Verify:

- `python -m pytest apps/web-server/tests/test_workspace_argv_never_logged.py tests/test_git_credentials.py -q -o asyncio_mode=auto`
- `python -m pytest tests/test_github_env.py -q` is fully green, M
  included.

Traps:

- `github_env` is a sync context manager entered around the awaits. It must
  not exit before `communicate()` returns.
- Risk 8: for github.com remotes the server token beats askpass. This is
  accepted.
- The `RuntimeError` check matches only the server token. A workspace
  credential in `extra_env` is a different value and does not trip it.

### 5. CHANGELOG, plan status, follow-ups, PR

- `CHANGELOG.md` `[Unreleased]` → `### Security`: the GitHub token is no
  longer in web-server child envs, it is passed per call through a temp
  `GH_CONFIG_DIR`, and `gh auth setup-git` is removed.
- Record any deviations in this plan, in the same commit as the code.
- Open the two follow-up issues (decision 13) and link them in the PR.
- The PR body links intent, spec and plan and names the steps the coder did.

Verify: the full gate under "Tests".

Traps:

- The commit scope may not contain `#`. Use
  `fix(security): pass the GitHub token per call via a temp GH_CONFIG_DIR (#1688)`.
- #1669/#1670 (`agent_kubejob.py`) do not overlap. #1671, #1672 and #1673
  do: rebase.

## Tests

### New `tests/test_github_env.py` (15 functions, 17 items)

Header:

- Insert `apps/backend` and `apps/web-server` into `sys.path`, as
  `tests/test_child_process_env.py` does.
- Import with `from core import git_credentials as gc` and
  `from core import child_env as ce`.
- Define `TOKEN = "ghp_1688FakeTokenSentinelDoNotLeak"  # gitleaks:allow`.
- Root `tests/` runs without `asyncio_mode=auto`, so N needs
  `@pytest.mark.asyncio`, as `tests/test_agent_event_hygiene.py` does.
  Without it N is not awaited and passes vacuously.

Autouse fixture:

- `setenv("GITHUB_TOKEN", TOKEN)` and `delenv("GH_TOKEN", raising=False)`.
- `setattr(tempfile, "tempdir", str(tmp_path))`.

| # | Test | Assertions | Mutation that turns it red |
|---|---|---|---|
| A | `test_token_rides_in_owner_only_hosts_file` | `with gc.github_env(ce.child_env()) as env`: dir = `env["GH_CONFIG_DIR"]`, its name starts `aif-gh-`. Dir mode `0o700`, `hosts.yml` mode `0o600`. `json.loads(hosts)["github.com"]` has `oauth_token == TOKEN`, `user == "x-access-token"`, `git_protocol == "https"`. No env value contains TOKEN. None of `GITHUB_TOKEN`/`GH_TOKEN`/`GIT_PASS` is a key. | `os.open` mode `0o644` |
| B | `test_inherited_github_credentials_are_stripped` | Base `{"PATH": ..., "GITHUB_TOKEN": "old", "GH_TOKEN": "old2", "GIT_PASS": "p"}`. None of the three keys is yielded. | Remove the strip. The values differ from TOKEN, so the fail-closed check does not fire. |
| C | `test_dir_removed[ok,error,timeout]` | Body does nothing / raises `RuntimeError` / runs `subprocess.run(["sleep","5"], timeout=0.1, env=env)` under `pytest.raises(TimeoutExpired)`. Afterwards the dir is gone and `tmp_path.glob("aif-gh-*")` is empty. | `rmtree` outside `finally` |
| D | `test_helper_entries_follow_hooks_path_and_existing_entry` | `GIT_CONFIG_COUNT=1`, `KEY_0=user.name`, `VALUE_0=x`, then `ce.child_env()`. Expect KEY_0 `user.name`; KEY_1 `core.hooksPath`; KEY_2 and KEY_3 exactly `credential.https://github.com.helper`; VALUE_2 `""`; VALUE_3 `!gh auth git-credential`; COUNT `"4"`. | Hard-coded `n = 0`; an unscoped `credential.helper` key (only this test catches it) |
| E | `test_malformed_config_count_does_not_raise` | `GIT_CONFIG_COUNT: "x"` does not raise. KEY_0/KEY_1 are the helpers and COUNT is `"2"`. | Drop the `except ValueError` |
| F | `test_token_in_extra_fails_closed` | `ce.child_env(extra={"AIF_PROBE": f"pre{TOKEN}"})` raises `RuntimeError`, and no `aif-gh-*` is left. | Delete the check |
| G | `test_no_token_passes_base_env_through` | `delenv GITHUB_TOKEN`. Then `env is base`, there is no `GH_CONFIG_DIR`, and no `aif-gh-*`. | Always create the dir |
| H | `test_helper_reset_keeps_store_from_saving_token` (skip unless `git` **and** `gh`) | Fake `HOME` with `.gitconfig` `[credential]\n\thelper = store`, plus `XDG_CONFIG_HOME`, `GIT_CONFIG_NOSYSTEM=1`, `GIT_TERMINAL_PROMPT=0`, and `GIT_CONFIG_GLOBAL` set to that `.gitconfig`. `child_env` passes a developer's `GIT_CONFIG_GLOBAL` through, and git then ignores `HOME/.gitconfig`: the `store` helper would never be configured and the mutation would stay green. `git credential fill` (`protocol=https\nhost=github.com\n\n`), then `git credential approve` with its output. rc 0, TOKEN in the fill output, `.git-credentials` does not exist. | Drop the `""` reset entry |
| I | `test_real_gh_reads_token_without_rewriting_hosts` (skip unless gh) | `gh auth token` prints TOKEN. Afterwards `os.listdir(dir) == ["hosts.yml"]` and the bytes are unchanged. | Key `token` instead of `oauth_token` |
| J | `test_sweep_removes_only_aif_gh_entries` | `aif-gh-old/hosts.yml` is removed and `keep-me/` stays. | `glob("*")` |
| K | `test_sweep_does_not_follow_symlink` | `aif-gh-link` → `victim/` containing `f`. The sweep does not raise and `victim/f` still exists. | `rmtree(p.resolve())` |
| L | `test_askpass_reads_password_from_file` | `with pws._git_askpass_env("oauth2", SECRET) as e`: `"GIT_PASS" not in e`. The pass file reads SECRET. Modes: pass 0600, script 0700, dir 0700. The dir prefix is `aif-gh-`. Running the script with `"Password for 'https://h'"` (env `{**ce.child_env(), **e}`) prints SECRET, and with `"Username for ..."` prints `oauth2`. The dir is gone after exit. | Script back to `$GIT_PASS`; yielding `GIT_PASS`; no `rmtree` |
| M | `test_web_server_has_no_env_token_path` | Every `*.py` under `apps/web-server/server` has no `keep=GITHUB_KEEP`, `"setup-git"` or `GIT_PASS"`. Hits are reported as `path:line`. (`GIT_PASS_FILE"` does not match.) | Re-add `env=child_env(keep=GITHUB_KEEP)` in `gh.py`. Red today: 23 hits. |
| N | `async test_login_route_spawns_nothing_when_token_set` | Patch `github_routes.shutil.which` to `"/usr/bin/gh"` and `asyncio.create_subprocess_exec` to `AsyncMock(side_effect=AssertionError)`. Expect `r["data"]["success"] is True` (the outer `success` is `True` on every return, so it proves nothing), `"GITHUB_TOKEN"` in `r["data"]["message"]`, and the mock not awaited. | Delete the guard. Without the `which` patch, the "gh not installed" return masks this on CI. |
| O | `test_run_gh_command_fails_closed_when_tmp_unwritable` | `tempfile.mkdtemp` raises `PermissionError`, and `server.services.gh.subprocess.run` fails if called. `run_gh_command(["pr","list"])["success"] is False`. | `with` outside the `try`; an env-token fallback. Must be `PermissionError`: `FileNotFoundError` hits the "gh not installed" branch first. |

### Edits to existing tests

- **`apps/web-server/tests/test_pr_endgame.py`:**
  - Lines 92-93 become
    `# #1688: auth is per call (github_env); no global helper.` plus
    `assert not r.saw("auth setup-git")`.
  - Add `test_default_runner_hands_gh_a_config_dir_not_the_token`. It sets
    `GITHUB_TOKEN`, removes `GH_TOKEN`, and sets `tempfile.tempdir` to
    `tmp_path`.
  - It patches `pe.subprocess.run` with a fake that records `env` and reads
    `Path(env["GH_CONFIG_DIR"], "hosts.yml").read_text()` at call time, then
    returns `CompletedProcess(argv, 0, "", "")`.
  - Assert TOKEN is in the hosts text and in no env value,
    `"GITHUB_TOKEN" not in env`, and no `aif-gh-*` is left.
  - This test catches a dropped wrap (a bare `env=child_env()` gives
    `KeyError`), which the grep guard cannot.
- **`apps/web-server/tests/test_merger.py:313`:** delete the
  `"auth setup-git"` route. This is cleanup only: FakeRunner returns rc 0
  for unmatched commands.
- **`tests/test_create_pr_fetches_branch.py:118-121`:** docstring only.
  `fake_gh` no longer shells out to `auth setup-git`. There is no assertion
  to change.
- **`apps/web-server/tests/test_workspace_argv_never_logged.py`:**
  - Add `pass_file: str | None` to `_Spawn` (81-87). The spy fills it at
    spawn time through a sync helper.
  - Replace the guard at 258-262 with
    `any(s.pass_file == _SECRET for s in seen)` and add
    `assert all("GIT_PASS" not in s.env for s in seen)`.
  - Docstring line 41 says `GIT_PASS_FILE`.
  - Lines 264-274 are unchanged.
- **`tests/test_git_credentials.py`:**
  - Docstring line 7 says `GIT_PASS_FILE`.
  - The fake at 80-85 reads `GIT_PASS_FILE` at spawn through a sync helper
    into `passes`.
  - Lines 104-106 become: `"ghp_secret" in passes`, `"GIT_PASS" not in`
    every env, and `GIT_ASKPASS` set on the spawn that carried it.
- **Unchanged, must still pass:**
  - `tests/test_git_argv_credential_backend.py`
  - `tests/test_child_process_env.py` (it imports `GITHUB_KEEP`, which stays
    exported)
  - `tests/test_workspace_fetch.py`
  - `tests/test_no_unscrubbed_spawn.py`
  - `tests/test_git_push_credential_helper.py`

### Commands and expected results

```
export PATH=/mnt/code/Source-home/GitHub/AIFactory/apps/backend/.venv/bin:$PATH
cd /mnt/code/Source-home/GitHub/AIFactory-1688
python -m pytest tests/test_github_env.py -q
#   17 passed here (gh and git are on PATH); 15 passed + 2 skipped without them
python -m pytest tests/test_git_credentials.py tests/test_create_pr_fetches_branch.py \
  tests/test_no_unscrubbed_spawn.py tests/test_child_process_env.py \
  tests/test_git_argv_credential_backend.py tests/test_git_push_credential_helper.py \
  tests/test_workspace_fetch.py tests/test_github_env.py -q
#   67 passed here (baseline without the new file: 50, measured)
python -m pytest apps/web-server/tests/test_pr_endgame.py apps/web-server/tests/test_merger.py \
  apps/web-server/tests/test_workspace_argv_never_logged.py -q -o asyncio_mode=auto
#   137 passed (baseline: 136, measured)
python -m pytest tests -q -m "not slow"
python -m pytest apps/web-server/tests -q -o asyncio_mode=auto
python -m pytest apps/backend -q -o asyncio_mode=auto
#   no new failures vs main
ruff format --check apps/backend apps/web-server scripts tests && ruff check apps/backend apps/web-server scripts tests
git add -A && python scripts/cq_ratchet.py --staged --ruff "$(command -v ruff)" --config standards/ruff.toml \
  --paths 'apps/backend/*.py' 'apps/web-server/*.py' 'scripts/*.py'
python scripts/cq_ratchet.py --staged --tool mypy --mypy "$(command -v mypy)" --config standards/mypy.ini \
  --paths 'apps/backend/*.py' 'apps/web-server/*.py' 'scripts/*.py'
python scripts/gen_autonomy_matrix.py --check
#   ok: tiers=10 overlay=12 val=8 paths=28 gates=3 controls=13 (or regenerate and commit)
grep -rn 'keep=GITHUB_KEEP\|"setup-git"\|GIT_PASS"' apps/web-server/server
#   no output
```

Mutation runs: apply each mutation from the table, run only that test with
`-k`, see it go red, then `git checkout -- <file>`.

Manual checks (spec Verification, all six), on this checkout and in a pod:

1. Start the server with `GITHUB_TOKEN` set and run a slow `gh pr view` or a
   `git fetch` of a large repo.
2. While it runs,
   `tr '\0' '\n' </proc/<child>/environ | grep -c "$GITHUB_TOKEN"` prints 0.
3. A PR create, the endgame push and fix push, and a workspace clone of a
   private repo all succeed.
4. With no token set, a developer's own gh login still works for `gh` and
   `git push`.
5. In the image, `gh --version` matches the format tested in I, and check 2
   passes.
6. `kill -9` the server during a call, restart it, and `ls /tmp/aif-gh-*`
   finds nothing.

## Rollback

1. `git revert <squash-sha>` reverts the code, tests and CHANGELOG
   together. The env token and `gh auth setup-git` come back. The Dockerfile
   global credential helper never changed, so pushes keep working. There is
   no data or schema change.
2. Remove leftover token dirs. The reverted code has no startup sweep, and
   a SIGKILL under the new code can leave dirs behind. The `/tmp` emptyDir
   survives a container restart inside a pod.
   - In a pod: `kubectl exec <pod> -- sh -c 'rm -rf /tmp/aif-gh-*'`, or
     delete the pod.
   - On dev machines: `rm -rf /tmp/aif-gh-*`.
3. Check the revert:
   `python -m pytest apps/web-server/tests/test_pr_endgame.py -q -o asyncio_mode=auto`
   passes, with `saw("auth setup-git")` asserted again.

## Drift from the spec (checkout wins)

- `_git_askpass_env` is at 137-176 (the spec says 141-177).
- The `routes/pr.py` setup-git block, with its comment, is at 279-292
  (the spec says 285-292).
- The `pr_endgame` setup-git comment is at 554-557, and the call is at 558.
- The `project_workspace_service` env build is at 467-470 (the spec says
  466-470).
- `tests/test_create_pr_fetches_branch.py` has no setup-git assertion, so
  that edit is docstring-only.
- The plan adds two test files the spec did not list:
  `tests/test_git_credentials.py`, whose `GIT_PASS` assert breaks, and the
  `_Spawn` field in `test_workspace_argv_never_logged.py`.
- `_Spawn` in `test_workspace_argv_never_logged.py` is at 81-87.
- The spec numbers its risks 1-5, 8, 6, 7. This plan cites them by the
  spec's numbers.
