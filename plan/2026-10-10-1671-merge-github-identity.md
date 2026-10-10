---
status: draft
issue: 1671
spec: spec/2026-10-10-1671-merge-github-identity.md
---

# Plan: AIFactory opens and merges its PRs as a GitHub App

Worktree `/mnt/code/Source-home/GitHub/AIFactory-1671`, branch
`feat/1671-merge-github-identity`, base HEAD `f4774fb4` (spec approved). The
branch contains #1691 (`1c2df772`), so `apps/backend/core/child_env.py` exists.

Five steps. Four edit files, so under the model split this goes to the
`coder` agent: start it with this path and step 1, then send each later step
to the same agent with `SendMessage`. Review with a fresh `opus` agent given
only this file and `git diff`.

## Approved decisions (self-contained summary)

1. **Identity: a GitHub App only, no machine user.** The App's own approvals
   are type `Bot`, and the gate already rejects bot approvals
   (`pr_endgame.py` `human_approval_head` :758-829, check `type == "User"` at
   :817). A machine user would be a second `User` identity, which the intent
   forbids.
2. **The maintainer's PAT leaves the pod.** With the App configured:
   - the chart refuses to render if the MCP GitHub PAT is also injected;
   - the server refuses to start if a PAT is in its boot env;
   - the minted token goes into the server's `os.environ`.
   `/proc/<pid>/environ` keeps the boot env even after `os.environ` is
   overwritten, so the PAT must never be there at boot. Claude agents still
   get both token names blanked (`apps/backend/core/auth.py:149-150`).
   Non-Claude runners and the GitHub MCP server do get the App token
   (accepted Risk 2).
3. **Scope: every PR action.** setup-git, push, `pr create`, the Copilot
   request, update-branch and merge all go through `_default_runner`
   (`pr_endgame.py:67-76`), which builds env per call with
   `child_env(keep=GITHUB_KEEP)` (:74). Update-branch therefore runs as the
   bot, so the maintainer is never the last pusher.
4. **Manual UI PRs are opened by the App too.** `services/gh.py`
   `run_gh_command` (:22-55, env at :39) reads the same process env. A human
   who wants own authorship opens the PR on GitHub.
5. **One global installation, configured by env/Helm.** Per-tenant
   credentials (Factory RFC-0020) are a follow-up.
6. **Variable names:** `AIFACTORY_GITHUB_APP_ID`,
   `AIFACTORY_GITHUB_APP_INSTALLATION_ID`, `AIFACTORY_GITHUB_APP_PRIVATE_KEY`.
   `GITHUB_BOT_TOKEN` (`apps/backend/runners/github/runner.py:93`) is left
   alone.
7. **Lands on top of #1691, ahead of #1688, with an env-based interim.**
   `child_env` reads `os.environ` per call and keeps `GH_TOKEN`/`GITHUB_TOKEN`
   for gh/git, so a token written to `os.environ` reaches every PR path with
   no spawn-site change. Edit no file #1691 rewrote except `pr_endgame.py`
   (one warning). #1688 later replaces the process-env write with per-call
   tokens.
8. **Unconfigured installs behave exactly as today**, plus one
   `logger.warning` when `watch_and_finish` waits on a human-approval gate
   with no App configured.
9. **No human fallback for the Copilot request**; `request_copilot_review`
   stays best-effort.
10. **No explicit self-approval exclusion.** #1663 decision B (do not
    configure the bot login) stands.
11. **The operator registers the App by hand** per the runbook: Contents R/W,
    Pull requests R/W, Metadata R; Workflows R/W only if PRs touch
    `.github/workflows`; no admin permissions.
12. **Done** = the maintainer's approval of the head commit merges a
    human-approval task end to end on AIFactory's own repo.

**Module design (`apps/web-server/server/services/github_app.py`, ~60 lines,
no new dependency).** `python-jose[cryptography]`
(`apps/web-server/requirements.txt:69`) signs the JWT, `httpx` (:57) calls
GitHub, `yaml` reads `hosts.yml`.

- Module globals `_app_id`, `_installation_id`, `_key` (all `None` at import);
  constants `_OK_DELAY = 1800`, `_RETRY_DELAY = 60`.
- `configured() -> bool` returns `_key is not None`. It reads module state,
  never the env (the key has been popped).
- `start()`, in this exact order:
  1. All-or-none on the three `AIFACTORY_GITHUB_APP_*` vars. None set:
     return (behaviour as today). Some set: `raise RuntimeError`.
  2. Refuse to start (`RuntimeError`) if any of `GITHUB_TOKEN`, `GH_TOKEN`,
     `GITHUB_PERSONAL_ACCESS_TOKEN`, or the var named by the operator config
     `github.tokenEnv` is **non-empty** in `os.environ` (an empty string is
     allowed: Kubernetes renders an empty Secret key as `""`). This also
     catches a PAT that `env_bootstrap` loaded from `.env`. Also refuse if
     `$GH_CONFIG_DIR/hosts.yml` (default `~/.config/gh/hosts.yml`) holds a
     `github.com` token, either `github.com.oauth_token` or
     `github.com.users.<u>.oauth_token`. A missing file is clean; other
     hosts are clean. The error message names the variable or file and
     never contains its value.
  3. Store the key in `_key` and `os.environ.pop("AIFACTORY_GITHUB_APP_PRIVATE_KEY")`.
  4. Mint once, fail closed: RS256 JWT with `iat=now-60`, `exp=now+540`,
     `iss=app_id`; `httpx.post("https://api.github.com/app/installations/{id}/access_tokens", ...)`
     then `raise_for_status()`; a missing or `""` token raises. On success
     write the token to both `os.environ["GH_TOKEN"]` and
     `os.environ["GITHUB_TOKEN"]`. Any error propagates and fails startup.
     Never write an empty string.
- `async refresh_loop(stop)`: copy `outbox.relay_loop`
  (`apps/web-server/server/services/outbox.py:328-336`). Loop
  `while not stop.is_set()`: **wait first** (`start()` already minted), then
  mint via `await asyncio.to_thread(_mint)`. Success: write both names, next
  delay `_OK_DELAY`. Failure: `logger.exception`, keep the current token,
  next delay `_RETRY_DELAY`. The wait is
  `with contextlib.suppress(TimeoutError): await asyncio.wait_for(stop.wait(), timeout=delay)`.
- The ceiling comment, above `refresh_loop`, verbatim except the citation
  fix:
  `# ponytail: process-wide token, 1h TTL, refreshed every 30 min; a build/Job snapshots it at spawn and gets >=30 min. Longer packed builds fail their final push quietly (core/workspace_fetch.py:91-108). Upgrade: per-call tokens with #1688.`
- Each replica mints its own token; nothing assumes `replicaCount: 1`.

**Spawn helpers are unchanged.** `child_env` and `make_subprocess_env` drop the
private key through `is_denied_env_key` (`PRIVATE_KEY` pattern,
`apps/backend/core/auth.py:156-159`); build Jobs use an allowlist
(`build_backend.py` `_PASSTHROUGH_BUILD_ENV` :259-327) that does not list it.
Tests pin both (Risk 8).

**The Q8 warning**, verbatim, at the start of `watch_and_finish`:

```python
if human_approval_required and not github_app.configured():
    logger.warning("[pr-endgame] human-approval gate but no GitHub App configured; the PR author is the maintainer, so this PR must be merged by hand")
```

**Helm.** `values.yaml` gets
`githubApp: {enabled: false, appId: "", installationId: "", secretName: "", privateKeyKey: private-key}`.
When enabled, `deployment.yaml` fails if `mcpCredentials.enabled` and
`mcpCredentials.providers.github` are both true (message names #1671),
`required`s `appId`, `installationId`, `secretName`, injects the two IDs as
quoted plain values and the key via `secretKeyRef`.

**Docs and compliance in the same PR:** CHANGELOG, environment reference,
a new runbook page, a note on #1663 spec decision B, the
`wiring.live_overlay` control objective plus a `claim`, and the regenerated
autonomy matrix.

**Decisions this plan makes where the spec was silent:**

- (a) Runbook: new page `docs/docs/concepts/github-app.md`, registered in
  `docs/sidebars.ts` right after `'concepts/mcp-credentials'` (:35).
- (b) `tokenEnv`: reuse `core.mcp_credentials._load_operator_config`
  (lru-cached, :87-90), imported **lazily inside `start()`** on one line:
  `from core.mcp_credentials import _load_operator_config  # noqa: PLC0415`
  (as `routes/mcp.py:55` does; `apps/backend` is on `sys.path` only after
  `server.utils.subprocess_env` is imported). Use
  `_load_operator_config().get("github", {}).get("tokenEnv")`.
- (c) The chart has no build-Job template. The helm "not in any Job" check
  covers only rendered chart Jobs/CronJobs (`cronjob-audit-anchor.yaml`); the
  real build-Job control is the Python allowlist, pinned by a unit test.
- (d) The child-env test lives in `tests/test_child_process_env.py` at the
  repo root.
- (e) The refresh loop mints through `asyncio.to_thread(_mint)`, not an
  `httpx.AsyncClient` path.
- (f) Spec Verification's `cd apps/web-server && pytest ...` omits
  `-o asyncio_mode=auto`; without it the async merge-gate tests do not run.
  The commands below include it.

**Code rules the tests depend on (do not deviate):**

- Call `httpx.post(...)` as a module attribute (`import httpx`), never
  `from httpx import post`; tests patch `github_app.httpx.post`.
- Call `asyncio.wait_for` as a module attribute (`import asyncio`); the
  refresh test patches `asyncio.wait_for`.
- In `pr_endgame.py` use `from server.services import github_app` and call
  `github_app.configured()`; importing the function directly makes the
  test's monkeypatch miss.
- `github_app` spawns no subprocess: read `hosts.yml` as a file, never shell
  out to `gh auth status`. This keeps `tests/test_no_unscrubbed_spawn.py`
  out of scope.
- Never log the token or the key.

**Accepted risks (carried from the spec, recorded in the matrix claim where
noted):**

1. Token expiry in long packed builds: a build or Job gets at least 30 min
   of token at spawn, no more guaranteed — the ceiling comment; fixed by
   #1688.
2. Agents can still use the App token (non-Claude runners via `RUNNER_KEEP`,
   GitHub MCP via its probe) — review must accept explicitly; **claim**.
3. PAT copies in project `.env` (`routes/github.py:129-131`) and the UI
   settings token (`routes/settings.py:2573-2575`, mapping :2634) — runbook
   plus **claim**.
4. GitHub Models (`apps/backend/providers/factory.py:195-196`) unsupported
   with the App — runbook.
5. Stored per-project clone PATs (`project_workspace_service.py:112-125`,
   235-290) — **claim** plus follow-up.
6. `oauth2@` username remotes unverified — the live test covers it.
7. A refresh failure keeps the old token and retries after 60 s.
8. The deny pattern is the only layer for the key if it were never popped —
   test pins it.
9. Concurrent `putenv` vs fork — noted, not fixed.
10. Repos without the App installed fail with the existing push warning
    (`pr_endgame.py:583`, `pr create` failure :605).
11. The Copilot request may fail (best-effort).

**Alternatives rejected (do not reintroduce):** `env=` at each call site; a
machine user or reusing `GITHUB_BOT_TOKEN`; overwriting a boot-time PAT
instead of refusing; starting anyway on a first-mint 5xx/network error;
relying only on the `PRIVATE_KEY` deny pattern; sleeping until
`expires_at - 30min`; lazy minting; a token cache class / `GitHubIdentity`
abstraction / PyJWT / githubkit; a sidecar or CronJob writing a Secret;
refusing human-approval tasks with no App; dropping `GITHUB_KEEP` from
`RUNNER_KEEP`; a warning when `GITHUB_BOT_TOKEN` is set alongside the App.

**Not changed (verified, leave alone):** `create_pr` (`pr_endgame.py:540-607`),
`request_copilot_review` (:617-637), `human_approval_head` (:758-829),
`merge_pr`/update-branch (:847-904, update-branch call :885),
`_default_runner` (:67-76); `services/gh.py`; `utils/subprocess_env.py`
(`RUNNER_KEEP` :32-40); `core/child_env.py` (`GITHUB_KEEP` :18);
`routes/pr.py` (:58, :480); `merger.py:368-369`; `build_backend.py`
(:185, :259-327); `core/git_credentials.py:52-54`; `core/auth.py`;
`core/mcp_credentials.py`; `core/workspace_fetch.py`; `routes/github.py`;
`routes/projects.py:951-952`; `routes/settings.py`;
`project_workspace_service.py`; `providers/factory.py`;
`scripts/gen_autonomy_matrix.py`.

## Steps

Setup for every command:
`cd /mnt/code/Source-home/GitHub/AIFactory-1671 && export PATH=/mnt/code/Source-home/GitHub/AIFactory/apps/backend/.venv/bin:$PATH`.
Baseline at f4774fb4: `test_pr_endgame_merge_gate.py` + `test_pr_endgame.py`
81 passed; `test_child_process_env.py` + `test_gen_autonomy_matrix.py` +
`test_no_unscrubbed_spawn.py` 40 passed; `gen_autonomy_matrix.py --check` ok.

Repo traps that apply to every step:

- Two ruff configs: CI runs the default config, `scripts/cq_ratchet.py` uses
  `standards/ruff.toml`; they disagree on import sorting. One name per
  import line; every `noqa` goes on a single-line import, never on an
  aliased multi-name import.
- Commit scopes must not contain `#`: `test(github-app): ... (#1671)`,
  `feat(github-app): ...`, `docs(github-app): ...`.
- Any change to `pr_endgame.py`, `main.py` imports or
  `control-objectives.toml` can move the autonomy matrix; run
  `python scripts/gen_autonomy_matrix.py --check` before each commit and, if
  it reports drift, regenerate and commit `docs/docs/compliance/autonomy-matrix.md`
  and `docs/static/compliance/autonomy-matrix.json` in the same commit.

1. **Tests first (test-only commit).** Write all tests below; they define the
   behaviour of steps 2-4.

   a. `apps/web-server/tests/test_github_app.py` (new, 21 cases).
   - Line 1: `# ruff: noqa: S105, S106`.
   - Imports, one per line: `asyncio`, `logging`, `os`, `time`, `httpx`,
     `pytest`, `from cryptography.hazmat.primitives.asymmetric import rsa`,
     `from cryptography.hazmat.primitives import serialization`,
     `from jose import jwt`, `from server.services import github_app`, then
     `from core import mcp_credentials as mc` **after** `github_app` (so
     `apps/backend` is on `sys.path`; any `# noqa: E402` on that single line).
   - Fixture `_isolate` (autouse):
     - `monkeypatch.delenv(name, raising=False)` for the three
       `AIFACTORY_GITHUB_APP_*`, `GITHUB_TOKEN`, `GH_TOKEN`,
       `GITHUB_PERSONAL_ACCESS_TOKEN` (restores originals; tokens written by
       `start()` do not leak).
     - `HOME` → tmp dir; `GH_CONFIG_DIR` → `tmp/gh` (otherwise the developer's
       real `hosts.yml` fails every test).
     - `mc.OPERATOR_CONFIG_PATH` → a tmp file path; `mc.reset_cache()` before
       and after (`_load_operator_config` is `lru_cache`d).
     - `monkeypatch.setattr` `github_app._key`, `_app_id`, `_installation_id`
       to `None`.
     - Patch `github_app.httpx.post` with a recorder whose default raises
       `AssertionError("unexpected POST")`.
   - Fixture `rsa_pem` (module scope): 2048-bit key; returns
     `(private_pem, public_pem)`.
   - Helpers: `_app_env(mp, pem)` sets `ID=123`, `INSTALLATION_ID=456` and the
     key; `_resp(token, status=201)` returns
     `httpx.Response(status, json={"token": token, "expires_at": "2099-01-01T00:00:00Z"}, request=httpx.Request("POST", "https://api.github.com/x"))`.
   - Cases:
     - `test_no_app_vars_is_a_noop`: returns; `configured()` False; no
       `GH_TOKEN`; no POST.
     - `test_partial_app_vars_refuse_to_start` parametrized over the 6 one-
       and two-var subsets: `RuntimeError`; no POST; `configured()` False.
     - `test_boot_pat_refuses_to_start[GITHUB_TOKEN|GH_TOKEN|GITHUB_PERSONAL_ACCESS_TOKEN]`:
       App set, var = `"ghp_boot"`: raises, message names the var,
       `"ghp_boot"` not in `str(exc)`, no POST.
     - `test_token_env_named_pat_refuses_to_start`: operator config file
       mode `0o600` (the perm-safe reader rejects others) with
       `{"github":{"tokenEnv":"OPS_GH_PAT"}}`, `OPS_GH_PAT` set: raises,
       names `OPS_GH_PAT`.
     - `test_empty_pat_var_does_not_refuse`: `GITHUB_TOKEN=""`, App set,
       mint returns `ghs_1`: `start()` succeeds.
     - `test_hosts_yml_github_token_refuses_to_start[legacy|nested]`:
       `github.com:\n  oauth_token: gho_x\n  user: u` and
       `github.com:\n  user: u\n  users:\n    u:\n      oauth_token: gho_x`:
       raises, no POST.
     - `test_hosts_yml_other_host_starts`: only
       `ghe.example.com: {oauth_token: x}`: succeeds.
     - `test_start_mints_and_moves_the_key`: key gone from `os.environ`;
       `GH_TOKEN` and `GITHUB_TOKEN` both `"ghs_1"`; `configured()` True; URL
       ends `/app/installations/456/access_tokens`; `Authorization` is
       `Bearer <jwt>`; `jwt.get_unverified_header(tok)["alg"] == "RS256"`;
       `jwt.decode(tok, public_pem, algorithms=["RS256"])` gives
       `str(iss) == "123"`, `exp - iat <= 600`, `iat <= time.time()`;
       `"ghs_1"` not in `caplog.text`.
     - `test_mint_error_at_start_raises[connect|http500|empty_token]`:
       POST raises `httpx.ConnectError` / returns 500 / returns 201 with
       `""`: raises; neither token name in `os.environ`.
     - `test_refresh_replaces_on_success_keeps_on_failure` (sync test):
       `start()` with `ghs_1`; then POST yields `ConnectError`, 201 `""`,
       201 `"ghs_2"`. `monkeypatch.setattr(asyncio, "wait_for", fake)` where
       the async fake closes the coroutine it gets, appends
       `(timeout, os.environ["GH_TOKEN"])` to a log, sets `stop` on the 4th
       call, and raises `TimeoutError`. Run
       `asyncio.run(github_app.refresh_loop(stop))`. Assert log ==
       `[(1800,"ghs_1"), (60,"ghs_1"), (60,"ghs_1"), (1800,"ghs_2")]` and both
       names end `"ghs_2"`.
     - `test_refresh_loop_exits_when_stopped`: `stop` already set;
       `asyncio.run(asyncio.wait_for(github_app.refresh_loop(stop), 1))`
       returns.

   b. `tests/test_child_process_env.py` after :98, +1 case:
   `test_github_app_private_key_never_reaches_a_child(secret_env, monkeypatch)`
   sets `AIFACTORY_GITHUB_APP_PRIVATE_KEY=-----BEGIN RSA PRIVATE KEY-----x` and
   asserts the name is absent from `child_env()`, `child_env(keep=GITHUB_KEEP)`,
   `make_subprocess_env()` and `make_subprocess_env(strip_anthropic_api_key=False)`.
   Passes today; pins Risk 8.

   c. `tests/test_build_backend_kubejob.py`, +1 case:
   `test_passthrough_never_carries_a_private_key`:
   `assert not any("PRIVATE_KEY" in v for v in bb._PASSTHROUGH_BUILD_ENV)`
   (reuses the file's `bb` import). Passes today.

   d. `apps/web-server/tests/test_pr_endgame_merge_gate.py`, from :99, +3
   async cases. Helper `_watch_once(**kw)` calls the **real**
   `pe.watch_and_finish(owner="o", repo="r", pr=7, auto_merge=True, review_fn=lambda: pe.ReviewState("changes_requested"), poll_interval=0, max_minutes=1, **kw)`
   (with `fix_fn=None` it returns on the first poll; do not use
   `_endgame_auto_merge`, which stubs it). Capture with
   `caplog.at_level(logging.WARNING, logger=pe.logger.name)`.
   - `test_human_gate_without_app_warns`: `pe.github_app.configured` →
     False, `human_approval_required=True`: exactly one record containing
     `"no GitHub App configured"`.
   - `test_human_gate_with_app_is_silent`: → True, required True: none.
   - `test_no_human_gate_is_silent`: → False, required False: none.

   e. `tests/helm/test_github_app_toggle.py` (new, 8 cases). Line 1
   `# ruff: noqa: S603, S607`. Every test `@pytest.mark.helm` (CI runs
   `pytest tests/helm/ -m helm`). Copy `_render`/`_render_expect_error` from
   `tests/helm/test_saml_scim_toggle.py:55-72`, add `_raw(chart_dir, sets) -> str`.
   Every render passes `postgres.externalSecretName=test-pg`. `ON` =
   `githubApp.enabled=true, appId=123, installationId=456, secretName=gh-app, privateKeyKey=pem`.
   - `test_default_render_has_no_github_app_env`: no `AIFACTORY_GITHUB_APP_`
     in stdout.
   - `test_enabled_render_wires_three_vars`: ID value is the string `"123"`,
     installation `"456"`; key entry has no `value` and
     `valueFrom.secretKeyRef == {"name": "gh-app", "key": "pem"}`.
   - `test_private_key_only_in_the_deployment`: `ON` +
     `audit.anchor.enabled=true` + `tenant.isolationEnabled=true`; assert at
     least one CronJob rendered; raw count of the key name is 1; it appears in
     `yaml.safe_dump(doc)` only for `kind == "Deployment"`; no ConfigMap has it.
   - `test_enabled_with_mcp_github_pat_fails`: `ON` +
     `mcpCredentials.enabled=true` + `mcpCredentials.providers.github=true`:
     fails, stderr contains `#1671`.
   - `test_enabled_with_mcp_on_but_github_off_renders`: `ON` + MCP on +
     `providers.gitlab=true`: renders.
   - `test_enabled_requires_field[appId|installationId|secretName]`: `ON`
     minus that field: fails, stderr names the field.

   → verify by
   `python -m pytest apps/web-server/tests/test_github_app.py apps/web-server/tests/test_pr_endgame_merge_gate.py -q -o asyncio_mode=auto`
   (new cases fail: module missing / no warning) and
   `python -m pytest tests/test_child_process_env.py tests/test_build_backend_kubejob.py -q`
   (pass) and `python -m pytest tests/helm/test_github_app_toggle.py -m helm -q`
   (fail, except the default-render case). Then `ruff format --check` and
   `ruff check` on the new/changed test files.
   Traps: the test file (a) fails at import until step 2; commit anyway as
   a test-only commit. Helm tests need `helm` and network for the conftest's
   `helm dep update` (outputs are gitignored). Commit:
   `test(github-app): pin App token, boot-PAT refusal and chart wiring (#1671)`.

2. **`apps/web-server/server/services/github_app.py` (new file, ~60 lines):**
   module exactly as in "Module design" and "Code rules" above, ceiling
   comment above `refresh_loop`. `hosts.yml` read with `yaml.safe_load`
   (`FileNotFoundError` → clean). → verify by
   `python -m pytest apps/web-server/tests/test_github_app.py -q -o asyncio_mode=auto`
   (21 passed), `ruff format --check` and `ruff check` on the file, then
   `git add` and `python scripts/cq_ratchet.py --staged`.
   Traps: no new dependency (`pyyaml` is not declared but is already
   imported by backend code, e.g. `apps/backend/core/language_descriptors.py`;
   `python-jose`, `httpx` are declared). No subprocess. Never log token or
   key. Error messages name, never print, the offending value. Do not
   catch the first-mint error in `start()`. Commit:
   `feat(github-app): mint installation token at boot (#1671)`.

3. **Wire startup and the warning.**
   - `apps/web-server/server/main.py:104`: right after `_make_non_dumpable()`
     in `lifespan` (:100-104), call `github_app.start()`, before any loop
     starts (`env_bootstrap` ran at import :22, so a `.env` PAT is visible).
     Import `from .services import github_app` next to the local imports.
   - `main.py:176-194`: after the outbox block, add
     `app.state.github_app_refresh_stop = None`,
     `app.state.github_app_refresh_task = None`; if `github_app.configured()`,
     create `stop = _asyncio.Event()` and
     `_asyncio.create_task(github_app.refresh_loop(stop=stop))`, same shape
     as outbox.
   - `main.py:288-317`: matching shutdown block: `stop.set()`,
     `await _asyncio.wait_for(task, timeout=5.0)`, cancel on
     `(TimeoutError, _asyncio.CancelledError)`.
   - `apps/web-server/server/services/pr_endgame.py:31-35`: add
     `from server.services import github_app` (single-name import line).
   - `pr_endgame.py:1006-1038`: after the `watch_and_finish` docstring closes
     (:1038), before the `review_fn` comment (:1039), insert the Q8 warning
     verbatim (see above).
   → verify by
   `python -m pytest apps/web-server/tests/test_github_app.py apps/web-server/tests/test_pr_endgame_merge_gate.py apps/web-server/tests/test_pr_endgame.py -q -o asyncio_mode=auto`
   (105 passed = 81 + 21 + 3), then the full
   `python -m pytest apps/web-server/tests -q -o asyncio_mode=auto` for
   lifespan regressions, then `python scripts/gen_autonomy_matrix.py --check`.
   Traps: `server.services.pr_endgame` is a matrix import-closure entry point
   (`scripts/gen_autonomy_matrix.py:465` `_ENTRY_POINTS`); the new import
   changes its closure, so expect to regenerate and commit both matrix files
   here. The closure must not reach `_MODEL_CLIENTS` (`httpx`, `jose`,
   `core.mcp_credentials` are fine). Edit no other file #1691 rewrote; do
   not touch `_default_runner`, `child_env`, `gh.py`. The `start()` call and
   the refresh-task wiring in `main.py` have **no unit test**, by design; the
   live Q12 check covers them — review must not count them as tested.
   Commit: `feat(github-app): start App token at boot and warn on hand merges (#1671)`.

4. **Helm chart.**
   - `charts/aifactory/values.yaml`: after the `mcpCredentials` block
     (:722-760), before `redis` (:784), add `githubApp:` with `enabled: false`,
     `appId: ""`, `installationId: ""`, `secretName: ""`,
     `privateKeyKey: private-key` and a comment (GitHub App for PR actions,
     #1671; mutually exclusive with the MCP GitHub PAT).
   - `charts/aifactory/templates/deployment.yaml`: after the
     `mcpCredentials` `{{- end }}` at :661, still inside `env:`, before
     `livenessProbe` at :662, a `{{- if .Values.githubApp.enabled }}` block
     **outside** the `mcpCredentials.enabled` condition: `fail` (idiom of
     :155-181) when `mcpCredentials.enabled` and
     `mcpCredentials.providers.github` are both true, message naming #1671;
     `required` on `appId`, `installationId`, `secretName`;
     `AIFACTORY_GITHUB_APP_ID` / `_INSTALLATION_ID` as `| quote`d values;
     `AIFACTORY_GITHUB_APP_PRIVATE_KEY` via
     `valueFrom.secretKeyRef` (`name: secretName`, `key: privateKeyKey`).
   → verify by `python -m pytest tests/helm -m helm -q` (all pass incl.
   `test_mcp_credentials_toggle.py`) and
   `helm template t charts/aifactory --set postgres.externalSecretName=test-pg,githubApp.enabled=true,githubApp.appId=1,githubApp.installationId=2,githubApp.secretName=s | grep -A3 GITHUB_APP`.
   Traps: quote the IDs (`--set` makes ints); match the env list
   indentation of :592-598. Commit:
   `feat(github-app): chart values and Deployment wiring (#1671)`.

5. **Docs, changelog, compliance.**
   - `CHANGELOG.md:20-21`: replace "Until AIFactory has its own bot identity,
     every such task merges by hand." with a sentence saying such tasks merge
     automatically when the GitHub App is configured and by hand otherwise.
     Add a #1671 entry under `[Unreleased]`.
   - `docs/docs/environment-reference.md:143-144`: rows for the three
     `AIFACTORY_GITHUB_APP_*` vars (key: Secret only, popped at boot); on the
     `GH_TOKEN`/`GITHUB_TOKEN` row note that with the App on the server sets
     both itself and refuses a PAT present at boot.
   - New `docs/docs/concepts/github-app.md` + `docs/sidebars.ts` entry after
     `'concepts/mcp-credentials'` (:35). Covers: registering the App with the
     Q11 permissions; installing it on target repos; the Secret and
     `githubApp` values; removing old PATs from project `.env` files and the
     UI settings token; removing the `gh` `hosts.yml` token; GitHub Models
     unsupported with the App; repos without the App fail with the existing
     warning; the token ceiling (#1688); rollback order (below).
   - `spec/2026-10-08-1663-contract-self-satisfy-gates.md:239`: append to
     decision B: the PR author is now the App (type Bot) per #1671; decision
     B (do not configure the bot login) stands.
   - `docs/compliance/control-objectives.toml:74-77`
     (`[controls."wiring.live_overlay"]`): objective adds that the PR author
     is the App bot and no PAT is in the pod; add a `claim` string recording
     accepted Risks 2, 3 and 5.
   - Regenerate: `python scripts/gen_autonomy_matrix.py`; commit
     `docs/docs/compliance/autonomy-matrix.md` and
     `docs/static/compliance/autonomy-matrix.json`.
   → verify by `python scripts/gen_autonomy_matrix.py --check`
   (`ok: ... controls=13`), `python -m pytest tests -q`, and
   `cd docs && npm run build` if the docs build is available.
   Traps: `claim` must be a string to pass the type check at
   `gen_autonomy_matrix.py:819`. Commit:
   `docs(github-app): runbook, changelog and matrix claim (#1671)`.

## Tests

From the worktree with the `PATH` above. All pass, no new skips (helm tests
skip only when `helm` is missing).

1. `python -m pytest apps/web-server/tests/test_github_app.py apps/web-server/tests/test_pr_endgame_merge_gate.py apps/web-server/tests/test_pr_endgame.py -q -o asyncio_mode=auto` → 105 passed.
2. `python -m pytest tests/test_child_process_env.py tests/test_build_backend_kubejob.py tests/test_no_unscrubbed_spawn.py tests/test_gen_autonomy_matrix.py -q` → baseline + 2.
3. `python -m pytest tests/helm/test_github_app_toggle.py tests/helm/test_mcp_credentials_toggle.py -m helm -q` → 8 new + existing.
4. `python scripts/gen_autonomy_matrix.py && python scripts/gen_autonomy_matrix.py --check` → ok, no diff left.
5. `ruff format --check apps/backend apps/web-server scripts tests && ruff check apps/backend apps/web-server scripts tests`, then `git add` and `python scripts/cq_ratchet.py --staged`.
6. Before the PR: `python -m pytest apps/web-server/tests -q -o asyncio_mode=auto` and `python -m pytest tests -q`.

**Mutation checks** (temporary local edit, confirm the named test fails,
`git checkout` the file; never commit):

| Mutation | Must fail |
|---|---|
| Return early when only `AIFACTORY_GITHUB_APP_ID` is unset | `test_partial_app_vars_refuse_to_start` |
| Drop `GITHUB_PERSONAL_ACCESS_TOKEN` from the checked names | `test_boot_pat_refuses_to_start[GITHUB_PERSONAL_ACCESS_TOKEN]` |
| Skip the `tokenEnv` lookup | `test_token_env_named_pat_refuses_to_start` |
| `name in os.environ` instead of non-empty | `test_empty_pat_var_does_not_refuse` |
| Only top-level `oauth_token` in `hosts.yml` | `test_hosts_yml_github_token_refuses_to_start[nested]` |
| Refuse whenever `hosts.yml` exists | `test_hosts_yml_other_host_starts` |
| Value in the error text | `test_boot_pat_refuses_to_start` |
| Mint before the PAT check | `test_boot_pat_refuses_to_start` (no POST) |
| Remove the key `pop` | `test_start_mints_and_moves_the_key` |
| Write only `GH_TOKEN` | `test_start_mints_and_moves_the_key` |
| `exp=now+700` / `iss=installation_id` | `test_start_mints_and_moves_the_key` |
| Catch the first-mint error | `test_mint_error_at_start_raises[connect]` |
| Drop `raise_for_status()` | `test_mint_error_at_start_raises[http500]` |
| Write the token unchecked | `test_mint_error_at_start_raises[empty_token]` |
| `_OK_DELAY` after failure / blank token on failure | `test_refresh_replaces_on_success_keeps_on_failure` |
| `while True:` | `test_refresh_loop_exits_when_stopped` |
| Drop `not github_app.configured()` | `test_human_gate_with_app_is_silent` |
| Drop `human_approval_required and` | `test_no_human_gate_is_silent` |
| `from server.services.github_app import configured` | `test_human_gate_with_app_is_silent` |
| Remove `PRIVATE_KEY` from `_AGENT_ENV_DENY_PATTERN` (`auth.py:158`) | `test_github_app_private_key_never_reaches_a_child` |
| Add the key to `_PASSTHROUGH_BUILD_ENV` | `test_passthrough_never_carries_a_private_key` |
| Remove `\| quote` on `appId` | `test_enabled_render_wires_three_vars` |
| Key in `value:` or a ConfigMap | `test_enabled_render_wires_three_vars` / `test_private_key_only_in_the_deployment` |
| Block inside `if .Values.mcpCredentials.enabled` | `test_enabled_render_wires_three_vars` |
| Delete the `fail` / fail on `mcpCredentials.enabled` alone | `test_enabled_with_mcp_github_pat_fails` / `test_enabled_with_mcp_on_but_github_off_renders` |
| Remove `required` on `installationId` | `test_enabled_requires_field[installationId]` |

**Done check after merge (Q12), on AIFactory's own repo:** the PR's
`.user.login` is `<slug>[bot]` and the bot is the last pusher; `gh pr checks`
green; the maintainer approves the head commit; the log shows
`approved by <maintainer> at <sha>` then the merge; `grep -c` of
`/proc/1/environ` finds 0 PAT/token values on the server pod at boot and 0
copies of the private key on agent and Job pods.

**PR:** links intent, spec and this plan; says which steps the coder did.

**Follow-up issues to file:** per-tenant `github_app` credentials with
RFC-0020 (Q5); Copilot review request with the App token (Q9); per-call
tokens under #1688; manifest-flow App registration (Q11); stored per-project
clone PATs (Risk 5).

## Rollback

The feature is off by default: no App vars means `start()` returns at once.

1. **Operational, no code change:** in one `helm upgrade`, set
   `githubApp.enabled=false` (or unset the three vars outside Helm), then
   restore the PAT (`mcpCredentials.providers.github=true` or
   `GITHUB_TOKEN`). Order matters: with the App still on, the chart `fail`
   and the boot-PAT check refuse the PAT. Human-approval tasks go back to
   hand merges and the Q8 warning appears.
2. **Crash loop at boot** (GitHub unreachable, bad key or IDs): same as 1.
   Refusing to start without a token is by design.
3. **Revoke access fast:** suspend or uninstall the App installation on
   GitHub; minted tokens die within 1 h, or at once when suspended. On a
   suspected key leak, delete the private key in the App settings and create
   a new Secret.
4. **Code:** `git revert <merge-sha>` of the single PR; the regenerated
   matrix, CHANGELOG, docs and the #1663 spec note revert with it. Then run
   `python scripts/gen_autonomy_matrix.py --check` and both pytest suites.
   No data migration: the token lives only in process env.
