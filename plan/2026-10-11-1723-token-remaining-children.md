---
status: draft
issue: 1723
spec: spec/2026-10-11-1723-token-remaining-children.md
---

# Plan: the GitHub token must not reach the four children #1688 left out

Line numbers checked against `fix/1723-token-remaining-children` at 7839c218
(`origin/dev` is an ancestor). The spec placed `github_env` at `git_credentials.py:64-112`,
but it now starts at `:65`, and `authed_push_url` starts at `:120`. The numbers
below are the current ones.

## Approved decisions (self-contained summary)

Approved decisions, carried over from the spec:

- **One mechanism.** Every GitHub credential for a child goes through the
  existing `github_env(base_env)` context manager
  (`apps/backend/core/git_credentials.py:64-109`). It writes the token into a
  0600 `hosts.yml` in a fresh 0700 `aif-gh-*` dir named by `GH_CONFIG_DIR`,
  strips `GITHUB_TOKEN`/`GH_TOKEN`/`GIT_PASS` from the env, resets inherited
  `credential.https://github.com.helper` entries, and points git at
  `!gh auth git-credential`. The dir is removed on exit. With no token it
  yields `base_env` unchanged. **No new credential helper and no second
  mechanism**, so there is no askpass and no `GIT_PASS_FILE`.
- **D1, agent runner.** The runner process may keep the token, so `RUNNER_KEEP`
  (`apps/web-server/server/utils/subprocess_env.py:47-55`, `*GITHUB_KEEP` at
  `:48`) is **unchanged**. The Claude CLI and its tools must not get it. The
  three SDK sites that build `ClaudeAgentOptions` with no `env=` get
  `env=get_agent_env_blanks()` (`apps/backend/core/auth.py:177-195`). The SDK
  merges `options.env` over `os.environ` (verified:
  `claude_agent_sdk/_internal/transport/subprocess_cli.py:430-434`), so the
  empty `GITHUB_TOKEN`/`GH_TOKEN` override the inherited values.
- **D2, worktree.** `_git_env()` stops keeping the token. `gh auth setup-git`
  is deleted, so nothing writes global git or gh config during a build. The one
  network call (the #1106 base fetch) runs under `github_env(_git_env())`.
  `GIT_TERMINAL_PROMPT=0` stays, so a missing credential fails fast. The
  image's build-time global helper (`Dockerfile:438`) is left alone.
- **D3, push URL.** `authed_push_url(url)` keeps its signature and its
  `(url, env)` contract. With a token and an https github.com URL it yields the
  **URL unchanged** plus `github_env({**child_env(), "GIT_TERMINAL_PROMPT": "0"})`.
  Otherwise it yields `url, child_env()`. `_ASKPASS_SCRIPT`, the temp askpass
  file, `GIT_ASKPASS`, `GIT_USER` and `GIT_PASS` go away. `GIT_PASS` stays in
  `_STRIPPED`. Callers are unchanged: `core/workspace_fetch.py:139` and
  `pfactory/tfactory_client.py:397`.
- **D4, web login.** Keep `gh auth login --web`, pointed at a managed
  `GH_CONFIG_DIR` = `get_data_dir() / "gh"` (`~/.aifactory/gh`), created 0700.
  A new `github_app.load_web_login()` reads the token from there with
  `gh auth token --hostname github.com` and publishes it through the existing
  `_publish()`, the same way the App token is published. It runs at startup
  (after `github_app.start()`, only when there is no token and no App) and
  after a successful login. `_hosts_yml_has_token()` also checks the managed
  dir.
- **Approver decision 1 (2026-10-13).** Existing web-login tokens in
  `~/.config/gh/hosts.yml` (or `$GH_CONFIG_DIR`) are **left in place**, and
  startup logs a **warning**. There is no migration and no refusal to start.
- **Approver decision 2.** The build Job runs on the root `Dockerfile` image,
  which installs `gh`, so the gh-on-PATH risk is closed. No work is needed.
- **D5, guard tests.** `test_web_server_has_no_env_token_path` scans
  `apps/web-server/server` **and** `apps/backend`, excluding tests, with these
  needles: `keep=GITHUB_KEEP`, `*GITHUB_KEEP`, `"setup-git"`, `GIT_PASS"`,
  `"GIT_PASS":`. Only two hits are allowed, each named by file: the
  `subprocess_env.py` `RUNNER_KEEP` line (D1) and the `git_credentials.py`
  `_STRIPPED` line. A second test asserts that every `ClaudeAgentOptions(...)`
  call in `apps/backend` (non-test) passes `env`.

Rejected and out of scope: dropping `GITHUB_KEEP` from `RUNNER_KEEP`,
migrating the old `hosts.yml`, removing web login, and the `/proc/<runner pid>`
exposure (accepted residual risk).

## Steps

Global traps (apply to every step):

- Backend venv: `/mnt/code/Source-home/GitHub/AIFactory/apps/backend/.venv/bin`
  (call it `$V`). Run tests from the worktree root with `$V/python -m pytest`.
- Before each commit, stage the files and run
  `$V/python scripts/cq_ratchet.py --staged`. It must pass, because the
  ruff/mypy ratchet is counted per changed file.
- `standards/coding-standards.md` is vendored. Never edit it.
- Do not edit `spec/` or `intent/`. If the code must deviate from this plan,
  edit this plan in the same commit as the code.
- Cite the step number in each commit message, e.g.
  `fix(git): authed_push_url on github_env (#1723, plan step 1)`.

1. **`apps/backend/core/git_credentials.py`: rebuild `authed_push_url` on `github_env` (D3).**
   - `:119-169` (`authed_push_url`): keep the decorator, the signature and the
     first branch (`:136-139`: no token or not `GITHUB_HTTPS_PREFIX`, which
     yields `url, child_env()`). Replace `:141-169` with:
     ```python
     with github_env({**child_env(), "GIT_TERMINAL_PROMPT": "0"}) as env:
         yield url, env
     ```
     Read `GITHUB_HTTPS_PREFIX` at call time, as it is now (a test
     monkeypatches it).
   - Delete `_ASKPASS_SCRIPT` (`:44-51`) and the comment above it. Delete the
     `import stat` (`:30`), which only the chmod used. Keep `tempfile` and
     `USERNAME`, because `github_env` uses both.
   - Leave `_STRIPPED` (`:60`) exactly as it is. It is an allowed guard hit.
   - Rewrite the module docstring (`:1-22`) and the `authed_push_url`
     docstring (`:121-135`). The token now reaches git through `github_env`'s
     per-call `GH_CONFIG_DIR` and the github.com-scoped `gh auth git-credential`
     helper. The URL is not rewritten. A non-github remote still gets plain
     `child_env()`. Keep the #1366 `/proc/<pid>/cmdline` rationale.

   → verify by `grep -nE 'ASKPASS|GIT_USER|"GIT_PASS":|import stat' apps/backend/core/git_credentials.py`,
   which should print nothing, and
   `$V/python -c "import sys; sys.path.insert(0,'apps/backend'); import core.git_credentials"`.

   Traps: the tests in step 7 break until step 7 is done, so commit steps 1 and
   7a together or run only the step-1 grep here. Ratchet with the file staged.

2. **`apps/backend/core/worktree.py`: no token in `_git_env`, no `setup-git`, fetch under `github_env` (D2).**
   - `:31` changes `from core.child_env import GITHUB_KEEP, child_env` to
     `from core.child_env import child_env`, and adds
     `from core.git_credentials import github_env`.
   - `:104-106` (`_git_env`) becomes
     `env: dict[str, str] = child_env(extra={"GIT_TERMINAL_PROMPT": "0"})`.
     In the docstring (`:93-103`), replace "the GitHub keep-list survives" with
     "no GitHub token (#1723): the one network call adds it per call via
     `github_env`".
   - Delete the call `self._setup_git_credentials()` (`:385`).
   - `:386-388`, the base fetch, becomes:
     ```python
     with github_env(_git_env()) as env:
         fetch = self._run_git(
             ["fetch", "origin", self.base_branch],
             timeout=_FETCH_TIMEOUT_SECONDS,
             env=env,
         )
     ```
     The error handling at `:389-417` stays outside the `with` and is unchanged.
   - Delete `_setup_git_credentials` (`:419-441`) completely.
   - `_run_git` (`:593-616`): add the parameter `env: dict[str, str] | None = None`
     after `timeout`, and pass `env=env if env is not None else _git_env()` to
     `subprocess.run`. Add one docstring line saying an explicit `env` replaces
     `_git_env()` (the base fetch passes the `github_env` one).

   → verify by `grep -nE 'GITHUB_KEEP|setup-git|_setup_git_credentials' apps/backend/core/worktree.py`,
   which should print nothing, and
   `$V/python -m pytest tests/test_worktree_fresh_base.py -q`, which should pass.

   Traps: `core/worktree.py` is listed in `tests/test_no_unscrubbed_spawn.py:65`.
   Keep every spawn using `_git_env()` or the `github_env` env, never bare
   `os.environ`. `tests/test_worktree_concurrent_lock.py:250` asserts
   `env["GH_TOKEN"] == "t"` and must flip in step 7. Ratchet staged.

3. **Blank the token for the three unscrubbed Claude SDK sites (D1).**
   - `apps/backend/runners/insights_runner.py:34` extends the import to
     `from core.auth import ensure_claude_code_oauth_token, get_agent_env_blanks, get_auth_token`.
     In `run_with_sdk` (`:132`), `ClaudeAgentOptions(` at `:183-195` gets
     `env=get_agent_env_blanks(),` (put it after `cwd=`).
   - `apps/backend/runners/github/services/followup_reviewer.py`: in
     `_run_ai_review` (`:569`), next to the function-local
     `from claude_agent_sdk import ClaudeAgentOptions, query` (`:680`), add
     `from core.auth import get_agent_env_blanks`. `ClaudeAgentOptions(` at
     `:698-707` gets `env=get_agent_env_blanks(),`.
   - `apps/backend/runners/ai_analyzer/claude_client.py`: after `:10`
     (`from core.outbound_scrub import ...`), add
     `from core.auth import get_agent_env_blanks`. In `_create_client` (`:103`),
     `ClaudeAgentOptions(` at `:124-134` gets `env=get_agent_env_blanks(),`.
   - Do **not** touch `RUNNER_KEEP`, `core/client.py` (it already passes `env`
     through `options_kwargs`, `:1059`) or `core/simple_client.py` (`:115`).

   → verify by `$V/python -m pytest tests/test_followup.py -q`, which should
   pass, and
   `grep -n "get_agent_env_blanks" apps/backend/runners/insights_runner.py apps/backend/runners/github/services/followup_reviewer.py apps/backend/runners/ai_analyzer/claude_client.py`,
   which should show 2 hits per file.

   Traps: in `followup_reviewer.py`, the `try/except ImportError` blocks at
   `:24-53` handle the package's own modules. `core.*` resolves from the
   backend root, as in `triage_engine.py`. Use the function-local import if
   ruff flags the top-level one (PLC0415 needs `# noqa: PLC0415` with a reason,
   following the pattern at `insights_runner.py:198-201`). Ratchet staged.

4. **`apps/web-server/server/services/github_app.py`: managed dir, login loader, legacy warning (D4, Approver decision 1).**
   - Add the imports `import shutil`, `import subprocess` and
     `from ..utils.subprocess_env import child_env, github_token`. Check that
     this does not create an import cycle: `subprocess_env` must not import
     `github_app`. Import `get_data_dir` function-locally
     (`from ..paths import get_data_dir  # noqa: PLC0415`), as
     `services/agent_credential.py:40` does, because `server.paths` runs a
     migration on import.
   - Add `_managed_gh_dir() -> Path`. It returns `get_data_dir() / "gh"` after
     `mkdir(mode=0o700, exist_ok=True)` and `chmod(0o700)`. Tests monkeypatch
     this function, because `paths.AI_FACTORY_DIR` is bound at import, so a
     `HOME` monkeypatch does not move it.
   - Refactor `_hosts_yml_has_token()` (`:42-56`). Move the body into
     `_dir_has_token(cfg: Path) -> bool` without changing it, including the
     "unreadable" `RuntimeError` that never quotes the token. Add
     `_default_gh_dir() -> Path`, which returns
     `Path(os.environ.get("GH_CONFIG_DIR") or Path.home() / ".config" / "gh")`.
     `_hosts_yml_has_token()` then returns
     `_dir_has_token(_default_gh_dir()) or _dir_has_token(_managed_gh_dir())`.
     `start()` (`:99-102`) is unchanged and now also refuses on a managed-dir
     token.
   - Add `warn_legacy_web_login() -> None`. If `_default_gh_dir()` is not
     `_managed_gh_dir()` and `_dir_has_token(_default_gh_dir())`, call
     `logger.warning(...)`. The warning names the path but never the token. It
     says the old web-login token stays there and is no longer used by
     AIFactory, and that the operator should log in again from the UI and
     remove the file. Catch the `RuntimeError` and warn without content.
   - Add `load_web_login() -> None`. Return early if `github_token()` or
     `configured()`, or if `shutil.which("gh")` is None. Otherwise run
     `subprocess.run([gh, "auth", "token", "--hostname", "github.com"], capture_output=True, text=True, timeout=15, check=False, env=child_env(extra={"GH_CONFIG_DIR": str(_managed_gh_dir())}))`,
     catching `OSError`/`subprocess.SubprocessError`, which mean return. On
     `returncode == 0` with non-empty `stdout.strip()`, call
     `_publish(token)` and `logger.info("published the web-login GitHub token")`.
     Never log stdout or stderr.
   - Update the module docstring (`:1-6`) with one line saying web login is
     published through the same `_publish()`.

   → verify by `$V/python -m pytest apps/web-server/tests/test_github_app.py -q`,
   which should pass with the existing tests unchanged (their `_isolate` fixture
   must also patch `_managed_gh_dir` to `tmp_path / "aif-gh"`, see step 7).

   Traps: `child_env` here is the web-server wrapper. With `runner=False` it
   strips `*_TOKEN`, so the `gh auth token` child gets no inherited token, and
   `GH_CONFIG_DIR` comes only from `extra`. Do not route this call through
   `github_env`, because there is no token yet. Ratchet staged.

5. **`apps/web-server/server/routes/github.py`: login writes to the managed dir and publishes on success (D4).**
   - `start_github_auth` (`:598`): the `create_subprocess_exec` at `:633-646`
     changes `env=child_env()` (`:645`) to
     `env=child_env(extra={"GH_CONFIG_DIR": str(github_app._managed_gh_dir())})`.
     Import `from server.services import github_app` next to `:25-28`.
   - `_monitor_gh_auth` (`:551`): in the `proc.returncode == 0` branch (`:560`),
     call `await asyncio.to_thread(github_app.load_web_login)` **before** setting
     `_gh_auth_status`. Then a status poll that sees success also sees the
     token published.
   - The "already authenticated" exit (`:685`, `proc.returncode == 0` with no
     device code) gets the same `await asyncio.to_thread(github_app.load_web_login)`
     before it returns. This is a planner addition. The spec names only the
     wait task, but this path is the same success, so the token would otherwise
     not be published until the next restart.

   → verify by `$V/python -m pytest tests/test_github_env.py -q -k login`,
   which should pass.

   Traps: the early return when `github_token()` (`:614-622`) stays, and
   `test_login_route_spawns_nothing_when_token_set` must keep passing. Do not
   add a wrapper for this call. If the ratchet flags the private-name import,
   rename `_managed_gh_dir` to `managed_gh_dir` everywhere and note it here.
   Ratchet staged.

6. **`apps/web-server/server/main.py`: startup loads the web login and warns on a legacy token (D4, Approver decision 1).**
   - After `github_app.start()` (`:110`), add:
     ```python
     github_app.warn_legacy_web_login()
     github_app.load_web_login()  # no-op when a token or the App is present
     ```
     This must come before any loop starts, for the same reason as the comment
     at `:109`.

   → verify by `$V/python -m pytest apps/web-server/tests -q -k "lifespan or main or github_app"`,
   which should pass.

   Traps: `load_web_login` spawns `gh` synchronously in the lifespan. This is
   acceptable because the timeout is 15 s and the work happens once. Do not
   make it async. Ratchet staged.

7. **Tests: update the askpass-era assertions and add the new coverage (spec Verification).**
   a. `tests/test_git_argv_credential_backend.py` (same commit as step 1):
      - `:83-132` (`test_real_git_child_never_publishes_the_token_in_proc_cmdline`):
        the vacuity guard becomes `Path(env["GH_CONFIG_DIR"], "hosts.yml").read_text()`
        containing `TOKEN`, plus `TOKEN not in "".join(env.values())`. Replace
        `b"x-access-token@" in cmdline` with `url.encode() in cmdline` and
        `url == f"{host}owner/repo.git"` (the URL is unchanged).
      - `:135-152`: the evil host gets no `GH_CONFIG_DIR` and no `GIT_ASKPASS`.
        The github.com remote yields `url == "https://github.com/o/r.git"` and
        a `hosts.yml` holding `TOKEN`.
      - `:155-163` (`test_askpass_helper_is_owner_only_and_removed_after_use`)
        is replaced by `test_gh_config_dir_is_owner_only_and_removed_after_use`.
        The dir has mode 0700, `hosts.yml` has mode 0600, and the dir does not
        exist after the `with`. It also asserts that `GITHUB_TOKEN`, `GH_TOKEN`
        and `GIT_PASS` are not in env and that `GIT_TERMINAL_PROMPT == "0"`.
      - `:166-194` and `:197-230`: the fake `subprocess.run` records
        `Path(kw["env"]["GH_CONFIG_DIR"], "hosts.yml").read_text()` **at call
        time**, because the dir is gone afterwards. Assert that the plain URL
        `https://github.com/o/r.git` is in argv, that the recorded `hosts.yml`
        holds `TOKEN` (the vacuity guard), and that `"GIT_PASS" not in env`.
      - Update the module docstring (`:1-25`) to describe the `GH_CONFIG_DIR` route.
   b. `tests/test_child_process_env.py:282-297`: replace the `GIT_PASS`
      assertion with `("GH_CONFIG_DIR" in env) == bool(token and url.startswith("https://github.com/"))`,
      and add `"GIT_PASS" not in env`. Fix the comment at `:295`.
   c. `tests/test_workspace_fetch.py:150-169`: `fake_run` records the
      `hosts.yml` text at call time. The URL is `"https://github.com/o/r.git"`,
      and the vacuity guard checks that `"ghs_faketoken"` is in the recorded
      `hosts.yml`. Fix the comment at `:165-166`.
   d. `tests/test_worktree_concurrent_lock.py:250`: change to
      `assert "GH_TOKEN" not in env` (D2).
   e. New tests in `tests/test_worktree_fresh_base.py`, using that file's
      existing repo fixtures:
      - `_git_env()` has no `GITHUB_TOKEN`/`GH_TOKEN` when both are set.
      - With `GITHUB_TOKEN` set, patch `core.worktree.subprocess.run` with a
        recorder that delegates to the real one. The `fetch origin <base>` call
        has `GH_CONFIG_DIR` in env, a `hosts.yml` holding the token (read at
        call time), and the `credential.https://github.com.helper` =
        `!gh auth git-credential` entry. No recorded argv contains `setup-git`
        (same pattern as `apps/web-server/tests/test_pr_endgame.py:95`).
   f. New `tests/test_agent_sdk_env_blanks.py`. Set `GITHUB_TOKEN=ghp_x` and
      `GH_TOKEN=ghp_x`, then capture the `ClaudeAgentOptions` kwargs at each
      site and assert `kwargs["env"]["GITHUB_TOKEN"] == ""` and `kwargs["env"]["GH_TOKEN"] == ""`:
      - `ai_analyzer.claude_client`: monkeypatch the module's
        `ClaudeAgentOptions`, `ClaudeSDKClient` and `wrap_client_outbound_scrub`
        with recorders, then call `_create_client(settings_file)`.
      - `insights_runner`: monkeypatch `insights_runner.ClaudeAgentOptions`
        with a recorder that raises a sentinel after recording, then call
        `run_with_sdk(...)` and catch the sentinel or the runner's own error
        handling.
      - `followup_reviewer`: monkeypatch `claude_agent_sdk.ClaudeAgentOptions`
        (recorder) and `claude_agent_sdk.query` (an async generator that
        yields nothing), then await `_run_ai_review(...)` on a minimal
        reviewer. Its `try` swallows the failure, and only the recorded kwargs
        matter.

      If a site's setup grows past roughly 30 lines, fall back to an AST
      assertion that the call passes `env=get_agent_env_blanks()`, and note the
      deviation here.
   g. `apps/web-server/tests/test_github_app.py`: the `_isolate` fixture
      (`:61-75`) also does
      `monkeypatch.setattr(github_app, "_managed_gh_dir", lambda: tmp_path / "aif-gh")`
      (and creates that dir). New tests:
      - A managed-dir `hosts.yml` token makes `start()` refuse.
      - `load_web_login()` publishes the token (patch `subprocess.run`/`shutil.which`
        and assert that the env passed has `GH_CONFIG_DIR` set to the managed
        dir and no `GITHUB_TOKEN`).
      - `load_web_login()` spawns nothing when `GITHUB_TOKEN` is set or when
        `configured()` is True.
      - `warn_legacy_web_login()` logs a warning that names the path and not
        the token (use `caplog`) when the default dir holds a token, and stays
        silent otherwise.
   h. `tests/test_github_env.py`: next to
      `test_login_route_spawns_nothing_when_token_set` (`:266`), add
      `test_login_route_uses_managed_gh_config_dir`. Patch `which` and
      `create_subprocess_exec` (a recorder that raises after recording) and
      assert `kwargs["env"]["GH_CONFIG_DIR"] == str(<patched managed dir>)`.

   → verify by running the step-7 files:
   `$V/python -m pytest tests/test_git_argv_credential_backend.py tests/test_child_process_env.py tests/test_workspace_fetch.py tests/test_worktree_concurrent_lock.py tests/test_worktree_fresh_base.py tests/test_agent_sdk_env_blanks.py apps/web-server/tests/test_github_app.py tests/test_github_env.py -q`,
   which should all pass.

   Traps: never put a real-looking token in a test without `# gitleaks:allow`
   (follow `tests/test_github_env.py:25`). `github_env` removes its dir on
   exit, so read `hosts.yml` inside the fake `run`, never after. Ratchet staged.

8. **`tests/test_github_env.py:255-263`: widen the guard (D5) and add the SDK-env guard.**
   - `test_web_server_has_no_env_token_path` scans
     `_SERVER` and `_ROOT / "apps" / "backend"`. Exclude any path with a
     `tests` part and any file named `test_*.py` or `conftest.py`, because
     `apps/backend/runners/github/test_*.py` and
     `apps/backend/security/test_output_dlp.py` live outside `tests/`. The
     needles are `("keep=GITHUB_KEEP", "*GITHUB_KEEP", '"setup-git"', 'GIT_PASS"', '"GIT_PASS":')`.
     Assert the exact allowed list, matched by file plus line content, not by
     line number:
     ```python
     # RUNNER_KEEP keeps the token in the runner process by approver decision (#1723 D1).
     allowed = {
         ("apps/web-server/server/utils/subprocess_env.py", "*GITHUB_KEEP,"),
         ("apps/backend/core/git_credentials.py", '_STRIPPED = ("GITHUB_TOKEN", "GH_TOKEN", "GIT_PASS")'),
     }
     ```
     Compare `{(rel_path, line.strip())}` against `allowed` for equality, so a
     new hit or a lost hit both fail.
   - Add `test_every_backend_agent_options_call_passes_env`. Use `ast.parse`
     on each non-test `apps/backend/**/*.py` file (same exclusions). For each
     `ast.Call` whose `func` is `Name` or `Attribute` named
     `ClaudeAgentOptions`, require a keyword `env`, or a `**` keyword
     (`kw.arg is None`, which covers `core/client.py:1153`
     `ClaudeAgentOptions(**options_kwargs)` with `"env"` at `:1059`). The
     failure message lists `path:lineno`. AST, not grep: the docstrings at
     `core/cache.py:249`, `agents/tools_pkg/mcp_catalog.py:17,98` and
     `agents/tools_pkg/__init__.py:25` mention `ClaudeAgentOptions(`.

   → verify by `$V/python -m pytest tests/test_github_env.py -q`, which should
   pass. Mutation check, by hand, once each and then revert:
   (i) restore `keep=GITHUB_KEEP` in `worktree.py`'s `_git_env`, so the guard fails;
   (ii) re-add `["gh", "auth", "setup-git"]` anywhere in `worktree.py`, so the guard fails;
   (iii) remove `env=` from `insights_runner.py`'s options, so the AST test fails.
   Record "mutation checks done" in the PR description.

   Traps: Ratchet staged.

9. **`CHANGELOG.md`: entry under `## [Unreleased]` → `### Security` (top of file, line 3).**
   Add a bullet before the #1717 one:
   `- **The GitHub token no longer reaches the worktree's git children, the push helper's env, three Claude agent SDK sites, or ~/.config/gh (#1723).**`
   Follow it with 3-5 wrapped lines. The worktree base fetch and the
   push/force-push get the token per call through `GH_CONFIG_DIR` (no askpass
   and no `GIT_PASS` env). `gh auth setup-git` is no longer run during builds.
   The insights, follow-up review and AI-analyzer agents get `GITHUB_TOKEN`
   and `GH_TOKEN` blanked. Web login now stores its token in `~/.aifactory/gh`.
   An existing `~/.config/gh/hosts.yml` token is left in place with a startup
   warning, so re-log in and remove it. Also add an accepted-residual-risk line:
   the runner process still holds the token by design, and the same-uid
   `/proc/<pid>/environ` exposure is out of scope.

   → verify by `head -20 CHANGELOG.md`.

   Traps: none beyond keeping the existing wrap style (about 80 columns, two-space indented continuation).

The autonomy matrix is **not affected**.
`scripts/gen_autonomy_matrix.py` is generated from `merge.merge_policy`, and
no merge-policy file changes. Do not regenerate it. Confirm with
`--check` under Tests.

## Tests

Run from `/mnt/code/Source-home/GitHub/AIFactory-1723`, with
`V=/mnt/code/Source-home/GitHub/AIFactory/apps/backend/.venv/bin`:

1. Targeted (spec Verification list):
   `$V/python -m pytest tests/test_github_env.py tests/test_git_credentials.py tests/test_git_argv_credential_backend.py tests/test_child_process_env.py tests/test_workspace_fetch.py tests/test_worktree_concurrent_lock.py tests/test_worktree_fresh_base.py tests/test_agent_sdk_env_blanks.py apps/web-server/tests/test_pr_endgame.py apps/web-server/tests/test_workspace_argv_never_logged.py apps/web-server/tests/test_github_app.py tests/test_no_unscrubbed_spawn.py tests/test_followup.py -q`
   should pass with 0 failures. `test_real_gh_reads_token_without_rewriting_hosts`
   may skip when `gh` is absent.
2. Full suites: `$V/python -m pytest tests -q` and
   `$V/python -m pytest apps/web-server/tests -q` should show no new failures
   compared with `origin/dev`. If a failure looks pre-existing, confirm it on a
   scratch `git worktree add <scratchpad>/dev origin/dev` before touching it.
3. Lint: `$V/ruff check apps/backend apps/web-server/server tests` should be
   clean on changed files, and `git add -A && $V/python scripts/cq_ratchet.py --staged`
   should pass.
4. `$V/python scripts/gen_autonomy_matrix.py --check` should exit 0 (unchanged).
5. Leak greps:
   - `grep -rnE 'GIT_ASKPASS|_ASKPASS_SCRIPT|setup-git' apps/backend --include=*.py | grep -v test`
     should print nothing.
   - `grep -rn "ClaudeAgentOptions(" apps/backend --include=*.py | grep -v test`
     should list only the sites covered by the AST guard.
6. Runtime check on the dev control plane (one build end to end; for the
   reviewer, not the coder):
   - The base fetch succeeds.
   - `stat -c %Y ~/.gitconfig` is the same before and after.
   - The `workspace_fetch` push and the TFactory force-push land.
   - During an insights run,
     `tr '\0' '\n' </proc/<claude cli pid>/environ | grep -E '^(GITHUB_TOKEN|GH_TOKEN)='`
     shows empty values.
   - `ls /tmp/aif-gh-*` is empty after the build.
7. Web login on a dev install with no token:
   - The device flow completes.
   - `~/.aifactory/gh/hosts.yml` (or the keyring) holds the token.
   - `~/.config/gh/hosts.yml` is not created.
   - `GET /api/github/auth/check` returns `authenticated: true`.
   - With a leftover `~/.config/gh/hosts.yml` token, startup logs the legacy
     warning once and does not quote the token.

## Rollback

- Code: `git revert` the implementation commits (steps 1-9), in one revert
  commit on `dev`. There is no schema, migration or config-format change, and
  no state is written that the old code cannot read.
- Web-login installs that logged in after this change have their token in
  `~/.aifactory/gh/hosts.yml`. After a revert, the old code reads only
  `~/.config/gh`. To restore, copy it with
  `install -m 600 ~/.aifactory/gh/hosts.yml ~/.config/gh/hosts.yml`, or log in
  again. Without that, gh calls are unauthenticated until re-login. They fail
  loudly, not silently.
- Hosts that relied on the removed `gh auth setup-git`: the old code re-runs it
  on the next build, so no manual step is needed. While this change is live,
  a local-dev host without a token that needs it runs `gh auth setup-git` once
  by hand (spec Risk).
- Leftover `aif-gh-*` dirs are swept at server start by the existing
  `sweep_github_dirs()` either way.
