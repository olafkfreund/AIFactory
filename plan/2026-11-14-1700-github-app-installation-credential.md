---
status: approved
issue: 1700
spec: spec/2026-11-14-1700-github-app-installation-credential.md
coders:
  1: codex   # tricky, security-sensitive core: per-installation minting, the token provider context, fail-closed rules
  2: codex   # wiring that depends on step 1's API and must keep tenant isolation; same implementer, warm context
  3: coder   # chart value, Helm test and docs: plain edits
reviewers:
  - codex    # independent read of the diff against this plan
  - agy      # second independent read; its spec review found the worktree-path and agent-token precedence gaps
---

# Plan: per-tenant GitHub App installations

Built on the #1973 branch (merged into this branch): `runner_github_env`,
`make_subprocess_env(github=...)`, `AGENT_GITHUB_TOKEN_VAR` already exist in
`apps/web-server/server/utils/subprocess_env.py`.

## Approved decisions (from the spec)

- One App key. The operator sets `AIFACTORY_GITHUB_APP_ORG_INSTALLATIONS`,
  a comma-separated list `<org_id>=<installation_id>`. Nothing a tenant can
  edit selects an installation. The stored-credential kind `github_app`
  stays rejected.
- A non-empty map puts the server in **multi-installation mode**, which
  fails closed: no process-wide token is published; a call gets a GitHub
  token only when it names a bound org; an unbound org, an unknown project
  path or an unwired code path gets no token.
- Without the map, everything behaves exactly as today.
- `start()` raises, before any HTTP call, when: the map variable is
  non-blank without the App; the variable is non-blank but yields no entry
  (for example `,`), so a typo can never fall back to the process-wide
  token; an entry has an empty or duplicate org id or an installation id
  that is not all ASCII digits; the map is set together with
  `AIFACTORY_AGENT_GITHUB_TOKEN`; or (with or without the map)
  `AIFACTORY_GITHUB_APP_INSTALLATION_ID` is not all ASCII digits.
- `installation_token(installation_id, *, min_ttl=900)`: cache per
  installation behind a lock; re-mint when less than `min_ttl` seconds are
  left; expiry from GitHub's `expires_at`, one hour when absent or
  unparsable; the id is re-checked before it enters the URL; a mint error
  propagates; never writes `os.environ`.
- The web server's own git and `gh` calls get the token through a
  `ContextVar` holding a token provider in
  `apps/backend/core/git_credentials.py`. With a provider set,
  `github_token()` returns `provider().strip()` and never reads the env.
- `org_github_token(org_id)`: no-op outside the mode; in the mode it always
  sets a provider: the org's installation token for a bound org, the empty
  string otherwise. So a nested block can never inherit another org's
  provider.
- Wired: clone at project add, runner spawns (build, spec, PR review), the
  build Job env, the PR endgame from context gathering to merge. Runner
  tokens are minted with `min_ttl=3300`.
- Not wired (no token in the mode; documented): `routes/github.py`,
  `routes/pr.py`, `routes/git.py`, `services/pr_data_service.py`,
  `services/copilot_dispatch_service.py`, `services/task_branch.py`,
  `services/merger.py`, `services/gh.py`, and the control plane's worktree
  fetch at `services/build_backend.py:1018`.
- Minting is a blocking HTTP call: async call sites run it in a thread.

Paths below are under `apps/web-server/server/` unless they start with
`apps/`, `tests/`, `charts/` or `docs/`.

## Steps

1. Core: minting, binding, token provider (coder: codex).

   a. `apps/backend/core/git_credentials.py`:
      - module level: `_token_provider: ContextVar[Callable[[], str] | None]`
        with default `None`;
      - `github_token()` (`:42-44`): if a provider is set return
        `provider().strip()`; else the env lookup as today;
      - new `use_github_token(provider)` context manager: `set`, `yield`,
        `reset(token)` in `finally`. Export nothing else; `github_env` and
        `authed_push_url` stay as they are.

   b. `services/github_app.py`:
      - `_ORG_MAP_VAR = "AIFACTORY_GITHUB_APP_ORG_INSTALLATIONS"`; module
        state `_org_installations: dict[str, str]`, `_tokens: dict[str,
        tuple[str, float]]`, `_tokens_lock = threading.Lock()`;
      - `_valid_installation_id(s) -> bool`: `s.isascii() and
        s.isdecimal()`;
      - `_parse_org_map(raw) -> dict[str, str]`: a blank `raw` gives `{}`.
        Otherwise split on `,`, skip empty items, each item `org=id` with
        both sides stripped; raise `RuntimeError` naming the org id on an
        empty org, a duplicate org, or an invalid id (for an item without
        `=` name the item position, not its text); raise as well when a
        non-blank `raw` yields no entry;
      - `_mint(installation_id) -> tuple[str, float]` (`:81-97`): the id is
        an argument; return the token and the expiry as epoch seconds
        (`datetime.fromisoformat` on `expires_at` with a trailing `Z`
        replaced by `+00:00`; `time.time() + 3600` when missing or
        unparsable); raise `RuntimeError` for an invalid id before any
        HTTP call;
      - `installation_token(installation_id, *, min_ttl=900) -> str` as
        decided; hold the lock around check, mint and store;
      - `multi_installation() -> bool`, `installation_for_org(org_id) ->
        str | None`, `token_for_org(org_id, *, min_ttl=900) -> str | None`
        (None outside the mode or for an unbound or `None` org);
      - `org_github_token(org_id)` context manager as decided, using
        `core.git_credentials.use_github_token`;
      - `start()` (`:143-171`): after the existing presence and PAT checks,
        validate the default installation id; parse the map (`os.environ`
        value, may be absent); if the map is non-empty and
        `os.environ.get(AGENT_GITHUB_TOKEN_VAR, "").strip()` is set, raise;
        if the map is set but no App variable is present, raise (this check
        goes before the early `return` at `:146`). In the mode: mint once
        per distinct bound installation through `installation_token` (fail
        early), do **not** call `_publish`. Outside the mode:
        `_publish(_mint(_installation_id)[0])` as today. On any failure
        clear `_key`, `_org_installations` and `_tokens` (fail closed, as
        the existing `except BaseException` does for `_key`);
      - `refresh_loop` (`:177-189`): `return` at once in the mode; else
        unchanged apart from `_mint`'s new signature.

   c. `project_registry.py`: `org_id_for_path(project_path: str | Path |
      None) -> str | None` after `load_projects` (`:47-53`). Fail closed:
      - return None for `None`, an empty string, or a path that is not
        absolute; same for a registry entry whose `path` is empty or not
        absolute (skip it);
      - normalise both sides with `os.path.normpath` only. Do **not** call
        `Path.resolve()` or `realpath`: a symlink inside one tenant's
        worktree must not lead into another tenant's project;
      - a match is equality or the entry path being a parent
        (`PurePath.is_relative_to`);
      - collect the `org_id` of every matching entry. If the matches name
        more than one org, or a matching entry has no `org_id`, return
        None (an ambiguous or unowned path gets no token). Otherwise
        return the one org id;
      - None when nothing matches or the file cannot be read.

   d. Tests (see Tests): extend `apps/web-server/tests/test_github_app.py`
      (its autouse `_isolate` fixture at `:61` patches `httpx.post`; also
      reset the new module state there) and `tests/test_github_env.py`; new
      cases for `org_id_for_path` in
      `apps/web-server/tests/test_project_registry_org.py`.

   → verify by
   `apps/backend/.venv/bin/pytest apps/web-server/tests/test_github_app.py apps/web-server/tests/test_project_registry_org.py -q -o asyncio_mode=auto`
   and `apps/backend/.venv/bin/pytest tests/test_github_env.py -q $GH_DESELECT`,
   where `GH_DESELECT` is
   `--deselect test_github_env.py::test_helper_reset_keeps_store_from_saving_token --deselect test_github_env.py::test_real_gh_reads_token_without_rewriting_hosts`
   (those two call the real `gh` and fail on this host on untouched code;
   CI runs them).
   Traps: `github_app.py` already imports `child_env, github_token` from
   `server.utils.subprocess_env`; import `AGENT_GITHUB_TOKEN_VAR` from the
   same module. `apps/backend` must not import from `apps/web-server`.
   Never log or put in an exception a token, the key or a JWT; error
   messages may name the org id and the variable names. `_mint` keeps the
   30 s timeout. The cq ratchet counts ruff and mypy findings per touched
   file against `origin/dev`. Do not stage or commit.

2. Wiring (coder: codex, same session as step 1).

   a. `utils/subprocess_env.py`:
      - `runner_github_env(project_path: str | Path | None = None)`: first
        line: if `github_app.multi_installation()` (lazy imports inside the
        function of `server.services.github_app` and
        `server.project_registry`, because `github_app` imports this
        module): `token = github_app.token_for_org(
        project_registry.org_id_for_path(project_path), min_ttl=3300)`;
        return the three fixed names set to it, or `{}` when it is None.
        The agent token and the ambient names are not consulted in the
        mode. Otherwise unchanged.
      - `make_subprocess_env(..., github=False, project_path=None)` passes
        the path to `runner_github_env`.
   b. The three `github=True` callers pass the project path and run the
      call in a thread (it can mint):
      `env = await asyncio.to_thread(make_subprocess_env, github=True,
      project_path=project_path)` at `services/agent_service.py:1114`,
      `services/agent_spec_creation.py:235` and
      `services/pr_review_service.py:259` (each is inside an `async def`
      that has `project_path`; check, and say so in the handback if one is
      not).
   c. `services/build_backend.py`: `build_job_env(oauth_token,
      project_path=None)` passes the path to `runner_github_env`; its one
      caller (find it with `grep -n "build_job_env(" `; it is in the
      `async` dispatch function that has `project_path`) becomes
      `await asyncio.to_thread(build_job_env, oauth_token, project_path)`.
   d. `routes/projects.py`, `add_project`: move the owner-org resolution
      (`org_id = DEFAULT_ORG_ID` … `resolve_owner_org_id`, `:649-653`) to
      before the clone block (`:563`), keep using that `org_id` for the
      registry entry; before the clone,
      `await asyncio.to_thread(github_app.token_for_org, org_id)` (warms
      the cache; a mint error here becomes the same 400 "Clone failed"
      path: catch `Exception` from the warm-up and raise the HTTP 400 with
      `client_error(logger, "Clone failed", e)`); run `clone_or_update`
      inside `with github_app.org_github_token(org_id):`, with or without a
      stored credential.
   e. `services/completion_orchestration.py`: inside the PR endgame `try`
      (`:283`), after the imports and before `if
      is_auto_pr_enabled(project_path):` (`:293`), compute
      `_org = org_id_for_path(project_path)`, warm with
      `await asyncio.to_thread(github_app.token_for_org, _org)`, and put
      the whole `if is_auto_pr_enabled(...)` block (through the
      `run_pr_endgame` call and its logging, to `:556`) inside
      `with github_app.org_github_token(_org):`. Only indentation changes
      inside the block.
   f. Tests (see Tests).

   → verify by
   `apps/backend/.venv/bin/pytest tests/test_child_process_env.py tests/test_github_env.py tests/test_build_backend_kubejob.py tests/test_env_passthrough_credentials.py -q $GH_DESELECT`
   and
   `apps/backend/.venv/bin/pytest apps/web-server/tests/test_github_app.py apps/web-server/tests/test_pr_endgame.py apps/web-server/tests/test_org_installation_wiring.py -q -o asyncio_mode=auto`
   Traps: `tests/test_github_env.py::test_web_server_has_no_env_token_path`
   pins the exact stripped lines that contain `runner_github_env(`; update
   the allowed set for the new signature and call lines. The AST call-site
   test from #1973 in `tests/test_child_process_env.py` expects
   `make_subprocess_env(github=True)` calls: it must now accept the call
   being passed to `asyncio.to_thread` with `github=True` and
   `project_path=` keywords in exactly those three files; adjust the test,
   do not weaken what it checks (the set of files and the `github` value).
   Existing callers of `build_job_env("tok")` in tests must keep working
   (the new parameter is optional). Do not stage or commit.

3. Chart and docs (coder: coder).
   - `charts/aifactory/values.yaml`, in the `githubApp` block (`:767-772`):
     add `orgInstallations: {}` with a comment: org id → installation id,
     as strings; a non-empty map turns on multi-installation mode; see the
     docs.
   - `charts/aifactory/templates/deployment.yaml`, inside the
     `githubApp.enabled` block after the private key entry (`:683`): when
     the map is non-empty, render env
     `AIFACTORY_GITHUB_APP_ORG_INSTALLATIONS` as `org=id` pairs joined by
     commas, in sorted key order (`range $org, $id := ...` iterates sorted).
     Each id must be a YAML **string** of digits: `fail` unless
     `kindIs "string" $id` and `regexMatch "^[0-9]+$" $id`. Numbers are
     rejected on purpose: Helm parses them as float64, which prints large
     ids in scientific notation and would round or truncate others. The
     failure message says to quote the id or use `--set-string`. Also
     `fail` when
     `agentGithubToken.secretName` is set together with a non-empty map.
   - `tests/helm/test_github_app_toggle.py`: absent when the map is empty;
     rendered as `acme=9002,default=9001` for two entries given with
     `--set-string`; `--set-string ...acme=12345678` renders `12345678`; an
     unquoted number (`--set ...acme=12345678`, and a values file with
     `acme: 9002.5`), `--set-string ...acme=abc` and `--set-string
     ...acme=1e7` each fail the render; the map with
     `agentGithubToken.secretName` fails the render.
   - `docs/docs/concepts/github-app.md`: new section "Several
     installations" before "Limits": what the map is and that only the
     operator sets it (an installation id is not a secret, so tenants must
     not be able to choose one); that every org, `default` included, must
     be bound, because an unbound org gets no GitHub token in this mode;
     that ids are written as quoted strings; that a project path registered
     under two orgs (one inside the other) gets no token;
     what is wired (project clone, build/spec/PR-review runners, build
     Jobs, the PR endgame) and that the interactive GitHub routes and the
     services listed in the decisions have no token in this mode yet
     (#1700 stays open for them); that it cannot be combined with the token
     for agent children; that a runner's token is fixed at spawn and lasts
     at least 55 minutes; how to roll back (empty the map). Add a Limits
     bullet pointing to the section.
   → verify by `apps/backend/.venv/bin/pytest tests/helm/test_github_app_toggle.py tests/helm/test_agent_github_token.py -q`
   Traps: `--set` turns `12345678` into a number, which this template
   rejects by design; the tests and the docs use `--set-string` or quoted
   YAML. Do not stage or commit.

## Tests

All with `httpx.post` patched; no App needed.

`apps/web-server/tests/test_github_app.py` (step 1):
- map parsing: valid two-entry map; duplicate org; empty org; `acme=12a`;
  `acme=../x`; an item without `=`; each raises before any HTTP call and
  the message has no installation token or key;
- the map without App variables raises; the map with
  `AIFACTORY_AGENT_GITHUB_TOKEN` raises; a default installation id of
  `12a` raises before any HTTP call (with and without the map);
- `installation_token`: second call within TTL does not mint; a cached
  token with less than `min_ttl` left is re-minted; `min_ttl=3300` re-mints
  a token with 50 minutes left; missing and malformed `expires_at` give a
  one-hour expiry; a mint HTTP error propagates and caches nothing; an
  invalid id raises without HTTP; two installations get different tokens;
  `GITHUB_TOKEN`/`GH_TOKEN` in `os.environ` are untouched;
- mode on: `start()` mints once per distinct installation and leaves
  `GITHUB_TOKEN`/`GH_TOKEN` unset; `refresh_loop` returns without minting;
  a failed bound mint at start raises and leaves `configured()` false;
- mode off: `start()` and `refresh_loop` behave as the existing tests
  assert (they keep passing unchanged except for `_mint`'s signature);
- a map variable of `,` or `" , "` raises before any HTTP call and nothing
  is published;
- `org_github_token`, with two bound orgs `acme` and `globex` on different
  installations with different tokens, and an ambient `GITHUB_TOKEN` set in
  the env to prove it is ignored: mode off → `github_token()` is the
  ambient token inside the block; mode on → `acme` gives acme's token,
  `globex` gives globex's; an unbound org and `None` give `""`; nested
  `acme` → unbound gives `""` inside and acme's token again after; nested
  `acme` → `globex` gives globex's inside and acme's after; two interleaved
  asyncio tasks for `acme` and `globex` each read their own token every
  time.

`tests/test_github_env.py` (step 1):
- `use_github_token`: `github_env` writes the provider's token to
  `hosts.yml` and ignores `GITHUB_TOKEN` in the env; an empty provider
  yields the base env without `GH_CONFIG_DIR`; the variable is reset after
  the block and after an exception inside it;
- nesting: inside A's block, a block with an empty provider gives no
  token, and A's token is back after the inner block;
- two `asyncio` tasks, each in its own block with its own token and
  interleaved with `await asyncio.sleep(0)`, each read their own token
  through `github_env` every time;
- a thread started with `asyncio.to_thread` inside the block sees the
  provider; a task created inside the block still sees it after the block
  has exited; a task created before the block does not.

`apps/web-server/tests/test_project_registry_org.py` (step 1): exact path;
a worktree path under a project; two entries of the same org, one inside
the other → that org; two entries of different orgs, one inside the other
→ None for a path under both; no match; `None`, `""` and a relative path →
None (with the process cwd inside a registered project, to prove cwd is
not used); a registry entry with an empty path is skipped; a path that
reaches another org's project only through a symlink (create one under
`tmp_path`) → the org of the path as written, not the symlink target; a
matching entry without `org_id` → None.

`tests/test_child_process_env.py` and `tests/test_build_backend_kubejob.py`
(step 2), with `github_app.multi_installation` and `token_for_org`
monkeypatched and a registry file under `tmp_path`:
- mode on, project of a bound org: `make_subprocess_env(github=True,
  project_path=p)` has the three fixed names equal to the org token; the
  ambient GitHub values and an agent token value appear in no env value;
  `token_for_org` was called with `min_ttl=3300`;
- mode on, unbound org, unknown path and no path: none of the GitHub names
  present;
- mode off: the #1973 cases still hold (existing tests unchanged);
- `build_job_env("tok", p)`: the same three situations; only `GITHUB_TOKEN`
  and `GH_TOKEN` are ever set.

`apps/web-server/tests/test_org_installation_wiring.py` (step 2):
- clone: with the mode on and `clone_or_update` replaced by a stub that
  records `core.git_credentials.github_token()`, `add_project` for a
  caller of a bound org records that org's token, with and without
  `gitCredentialId`; for an unbound org it records `""`; with the mode off
  it records the ambient token. (Call the route function directly the way
  the existing project-route tests do; if `add_project` cannot be called
  without a large fixture, test a small helper extracted for the clone
  call instead and say so in the handback.)
- endgame, through the real wiring: drive the function in
  `services/completion_orchestration.py` that contains the PR endgame block
  for a project of `acme` (registry file under `tmp_path`), with
  `is_auto_pr_enabled` true and `gather_pr_context`, `run_pr_endgame`
  replaced by stubs that record `core.git_credentials.github_token()`:
  both record acme's token; for a project of an unbound org both record
  `""`. If that function cannot be driven without a large fixture, move
  the endgame block into a small module-level coroutine, call it from the
  old place, test that, and say so in the handback. Plus: `run_pr_endgame`
  inside `org_github_token("acme")` with `watch_and_finish` stubbed: a
  background task created inside the block still reads acme's token after
  the block exits.
- Job dispatch: a source test that the one call of `build_job_env` in
  `services/build_backend.py` passes `project_path` as its second
  argument and runs through `asyncio.to_thread`.

`tests/helm/test_github_app_toggle.py` (step 3) as listed in step 3.

Full run (lead, after all steps):
`apps/backend/.venv/bin/pytest tests/test_child_process_env.py tests/test_github_env.py tests/test_env_passthrough_credentials.py tests/test_build_backend_kubejob.py tests/test_mcp_credentials.py tests/helm/test_github_app_toggle.py tests/helm/test_agent_github_token.py -q`
and
`apps/backend/.venv/bin/pytest apps/web-server/tests/test_github_app.py apps/web-server/tests/test_project_registry_org.py apps/web-server/tests/test_org_installation_wiring.py apps/web-server/tests/test_pr_endgame.py apps/web-server/tests/test_pr_endgame_merge_gate.py -q -o asyncio_mode=auto`;
all pass except the two real-`gh` tests named in step 1. Ratchet as CI runs
it (`.github/workflows/cq-ratchet.yml:109-135`) with `--base origin/dev`:
no regression.

Not tested: a real second installation (needs the owner's App and a second
account).

## Deviations

Recorded after the final review (Fable, Codex):

- `GH_DESELECT` uses node ids relative to pytest's rootdir (`tests/`); the
  ids first written here deselected nothing.
- `installation_token` reads a fresh cached token without the lock, and the
  `org_github_token` provider asks for `min_ttl=60`, not 900: a token read on
  the event loop must not wait behind another org's mint. A block that
  outlives its warmed token still mints inline once.
- The provider logs a mint error and returns no token instead of raising
  (`installation_token` itself still propagates): `github_token()` never
  raised before, and the background PR watcher has no handler for it. The
  endgame warm-up logs a mint error at warning level and carries on.
- `org_id_for_path` skips registry entries that are not objects with a
  string path.
- The chart fails the render when `orgInstallations` is set without
  `githubApp.enabled`.
- `build_job_env` has a second caller, `services/spec_job_backend.py`, which
  passes no path: Kubernetes spec Jobs get no GitHub token in the mode. The
  docs list it with the unwired services.
- The merge of `dev` duplicated the `agentGithubToken` block in the chart's
  values and deployment template; the copies were removed.

## Rollback

Unset `AIFACTORY_GITHUB_APP_ORG_INSTALLATIONS` (chart:
`githubApp.orgInstallations={}`) and restart: the server is back to one
installation with a process-wide token. Reverting the PR removes the code;
there is no data or schema change.
