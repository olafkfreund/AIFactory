---
status: approved
issue: 1671
intent: intent/2026-10-10-1671-merge-github-identity.md
---

# Spec: AIFactory opens and merges its PRs as a GitHub App

## Design

AIFactory gets one global GitHub App installation. At boot the server mints
an installation token and writes it into its own `os.environ` as `GH_TOKEN`
and `GITHUB_TOKEN`, then refreshes it every 30 minutes. Every existing
consumer already reads the process env when it spawns `gh`/`git` or looks up
the token, so no call site changes. The PR author becomes `<app>[bot]` (type
Bot), so the maintainer's approval of the head commit clears the
`human-approval` gate (`apps/web-server/server/services/pr_endgame.py:805-812`).
The App's own approvals are type Bot and can never count. The maintainer's PAT
is no longer in the pod.

Paths below are relative to `apps/web-server/server/` unless they start with
`apps/`, `charts/`, `tests/`, `docs/`, `spec/` or `scripts/`.

**Base.** PR #1691 (#1680) merged to `dev` on 2026-10-10 as `1c2df772`, one
commit ahead of this branch. It routes every `gh`/`git` spawn the endgame, the
manual route and the build backend use through `child_env(keep=GITHUB_KEEP)`
(`apps/backend/core/child_env.py` on `origin/dev`), which copies `os.environ`
per call, and it drops every `is_denied_env_key` name, `PRIVATE_KEY` included,
from every child. This spec builds on that. The branch is rebased onto
`origin/dev` before implementation. Line numbers below are from this checkout;
files #1691 touched are cited by symbol where their lines move.

### Decisions on the intent's open questions

Each item is the default this spec builds on. Reject any of them in review.

1. **Q1, identity type: GitHub App only, no machine user.** App approvals are
   type Bot, and the gate already rejects Bot approvals (`pr_endgame.py:805-812`;
   `spec/2026-10-08-1663-contract-self-satisfy-gates.md:81-85`). A machine user
   is type User and would break the "no two User identities" constraint
   (intent:73-76).
2. **Q2, does the PAT leave the pod: yes.** With the App configured:
   (a) the chart refuses to render if the MCP GitHub PAT is also injected;
   (b) the server refuses to start if a PAT is in its boot env;
   (c) the minted token goes into the server's `os.environ`. `/proc/<pid>/environ`
   keeps the boot env even after `os.environ` is overwritten, so the PAT must
   never be there. Claude agents still get both names blanked
   (`apps/backend/core/auth.py:149-150`). Non-Claude runners and the GitHub MCP
   server do get the App token; see Risk 2.
3. **Q3, scope: everything.** setup-git, push, `pr create`, Copilot request,
   update-branch and merge all go through `_default_runner`, which builds its
   env from the current process env on every call (`pr_endgame.py:66-68,
   532-598, 610-622, 862-880`; on `dev`, `child_env(keep=GITHUB_KEEP)`). Update-branch
   runs as the bot, so the maintainer is never the last pusher (intent:94-96).
4. **Q4, manual UI PRs are opened by the App too.** `run_gh_command` reads
   the same process env (`services/gh.py:32-38`; `routes/pr.py:464`). Keeping them
   human-authored needs the PAT in the pod. A human who wants their own
   authorship opens the PR on GitHub.
5. **Q5, one global installation**, configured by env/Helm. Per-tenant
   `github_app` credentials aligned with Factory RFC-0020 are a follow-up.
6. **Q6, new variable names:** `AIFACTORY_GITHUB_APP_ID`,
   `AIFACTORY_GITHUB_APP_INSTALLATION_ID`, `AIFACTORY_GITHUB_APP_PRIVATE_KEY`.
   `GITHUB_BOT_TOKEN` (`apps/backend/runners/github/runner.py:93`) is left
   alone.
7. **Q7, land on top of #1691, ahead of #1688, with the env-based interim.**
   #1691 is merged. Its `child_env` reads `os.environ` per call and keeps
   `GH_TOKEN`/`GITHUB_TOKEN` for `gh`/`git`, so a token written to
   `os.environ` reaches every PR path with no spawn-site change. This spec
   edits no file #1691 rewrote except `pr_endgame.py` (one warning in
   `watch_and_finish`). #1688 (open) moves to per-call tokens later and
   replaces the process-env write.
8. **Q8, unconfigured installs behave as today**, plus one `logger.warning`
   when `watch_and_finish` waits on `human-approval` with no App configured.
9. **Q9, no human fallback for the Copilot request.** `request_copilot_review`
   stays best-effort (`pr_endgame.py:609-612`). A follow-up checks whether an
   App token can request `copilot-pull-request-reviewer[bot]`.
10. **Q10, no explicit self-approval exclusion.** It is moot without a machine
    user; #1663 decision B (do not configure the bot login) stands.
11. **Q11, the operator provisions the App by hand**, following the docs:
    Contents R/W, Pull requests R/W, Metadata R; Workflows R/W only if PRs
    touch `.github/workflows`; no admin. A manifest-flow registration is a
    follow-up.
12. **Q12, done** means the maintainer's approval of the head commit merges a
    `human-approval` task end to end on AIFactory's own repo. Other repos only
    need the App installed.

### Components

**1. New module `services/github_app.py` (about 60 lines).** It adds no
dependency: `python-jose[cryptography]` (`apps/web-server/requirements.txt:69`)
signs the JWT and `httpx` (`requirements.txt:57`) calls GitHub.

- `start()`, called from `lifespan` in `main.py` right after
  `_make_non_dumpable()` (about `:106`), before any loop starts:
  1. **All or none.** If one or two of the three `AIFACTORY_GITHUB_APP_*`
     vars are set, refuse to start. A missing key must not silently mean
     "App off, PAT mode". If none is set, return; behaviour is as today.
  2. **Refuse a boot-time PAT.** Refuse to start if any of `GITHUB_TOKEN`,
     `GH_TOKEN`, `GITHUB_PERSONAL_ACCESS_TOKEN`, or the variable named by the
     operator config `github.tokenEnv` is non-empty. The MCP GitHub probe reads
     all of these (`apps/backend/core/mcp_credentials.py:185-203`), so checking
     only the first two would let a PAT back in. The check reads `os.environ`,
     so it also catches a PAT that `env_bootstrap.py:27` loaded from
     `apps/web-server/.env` before `lifespan` runs. Also refuse if
     `hosts.yml` (`$GH_CONFIG_DIR`, default `~/.config/gh`) holds a
     `github.com` token (left by an earlier `gh auth login`,
     `routes/github.py:620-623`): `gh` falls back to that file whenever
     `GH_TOKEN` is empty.
  3. **Move the key out of the env.** Read the private key into module memory
     and `os.environ.pop("AIFACTORY_GITHUB_APP_PRIVATE_KEY")`. No child spawned
     after this inherits it, whichever spawn helper it uses. The second layer
     already exists: on `dev`, `child_env` drops every name matching the
     `PRIVATE_KEY` deny pattern (`apps/backend/core/auth.py:156-158`). The boot copy
     in `/proc/1/environ` is not readable by agents because the process is
     non-dumpable (#1679).
  4. **Mint once, fail closed.** Sign an RS256 JWT (`iat=now-60`,
     `exp=now+540`, `iss=app_id`), `POST
     /app/installations/{id}/access_tokens`, and write the token to
     `os.environ["GH_TOKEN"]` and `os.environ["GITHUB_TOKEN"]`. Any error fails
     startup, so the server never runs with no token (and so never falls back
     to `hosts.yml`). An empty string is never written.
- `refresh_loop(stop)`: one task created with the same `stop`/`create_task`
  pattern as `outbox_relay` (`main.py:180-190`), with a matching `stop.set()`
  at shutdown. It sleeps 30 minutes after a successful mint and 60 seconds
  after a failed one, and on failure keeps the current, still-valid token and
  logs the exception. The 60-second retry matters: with a fixed 30-minute retry,
  one failure at minute 30 would let the token expire before the next try.
- `configured() -> bool` reads module state set by `start()`, not the env,
  because the key has been popped.
- The ceiling is marked in code:
  `# ponytail: process-wide token, 1h TTL, refreshed every 30 min; a build/Job snapshots it at spawn and gets >=30 min. Longer packed builds fail their final push quietly (workspace_fetch.py:89-106). Upgrade: per-call tokens with #1688.`
- Each replica mints its own token. GitHub allows several tokens per
  installation at once, so nothing assumes `replicaCount: 1`.

**2. No change to the spawn helpers.** On `dev`, `make_subprocess_env` and
`child_env` both drop any `PRIVATE_KEY` name through `is_denied_env_key`, so
the Codex and Antigravity spawns (`insights_providers/codex_provider.py:86`,
`antigravity_provider.py:102`) and Claude agents (`apps/backend/core/auth.py:156-158`)
never get the key, even before the pop. Build Jobs never get it: the
passthrough is an allowlist (`services/build_backend.py:259-300`). A test pins
this (Verification).

**3. `services/pr_endgame.py`, at the start of `watch_and_finish` (`:998`).**
One warning (Q8):

```python
if human_approval_required and not github_app.configured():
    logger.warning("[pr-endgame] human-approval gate but no GitHub App configured; "
                   "the PR author is the maintainer, so this PR must be merged by hand")
```

**4. Helm chart.**
- `charts/aifactory/values.yaml`, next to `mcpCredentials` (`:725-745`):
  `githubApp: {enabled: false, appId: "", installationId: "", secretName: "", privateKeyKey: private-key}`.
  Off by default.
- `charts/aifactory/templates/deployment.yaml`, next to `:592-598`, when
  `githubApp.enabled`:
  - `fail` if `mcpCredentials.enabled` and `mcpCredentials.providers.github`
    are both true, with a message naming #1671 (Q2a). Same `fail` idiom as
    `:155-181`.
  - `required` on `appId`, `installationId` and `secretName`.
  - Inject the two IDs as plain values and the key through `secretKeyRef`.

**5. Unchanged, by design.** `create_pr`, `merge_pr`, update-branch,
`request_copilot_review` and `_default_runner` (`pr_endgame.py:66-68,
532-622, 862-880`); `services/gh.py:32-38`; `utils/subprocess_env.py` and
`apps/backend/core/child_env.py`; `routes/pr.py:464`;
`merger.py:368`; the Job passthrough (`build_backend.py:287-288`, read at
dispatch); `apps/backend/core/git_credentials.py:52`; `apps/backend/core/auth.py`.
The gate logic is untouched: PR author and reviewer logins both come from REST
`.user.login`, which is `<slug>[bot]` for the App.

**6. Docs and compliance, in the same PR (an intent constraint).**
- `CHANGELOG.md:5-10`: replace "until AIFactory has its own bot identity".
  Merges are automatic with the App configured and by hand otherwise.
- `docs/docs/environment-reference.md:143-144`: the three new variables; with
  the App on, the server sets `GH_TOKEN`/`GITHUB_TOKEN` itself and refuses a
  boot-time PAT.
- `spec/2026-10-08-1663-contract-self-satisfy-gates.md`, decision B: a note
  that the PR author is now the App (type Bot) and decision B stands (Q10).
- `docs/compliance/control-objectives.toml:74-77`, control
  `wiring.live_overlay`: the `objective` says the PR author is the App bot
  and the PAT is absent from the pod, and a new `claim` field (rendered as the
  matrix's last column, `scripts/gen_autonomy_matrix.py:785, 849`) records the
  accepted residuals: Risks 2, 3 and 5.
- An operator runbook section: App registration with the Q11 permissions,
  installation on target repos, the Secret, removing old PATs from project
  `.env` files and the UI settings token, and that GitHub Models does not work
  with the App (Risk 4).
- Rerun `scripts/gen_autonomy_matrix.py`. The TOML edit changes the
  `wiring.live_overlay` row, so commit the regenerated
  `docs/docs/compliance/autonomy-matrix.md` and
  `docs/static/compliance/autonomy-matrix.json` in the same PR; the required
  `autonomy matrix` check (`--check`) fails otherwise.

**Follow-ups to file:** per-tenant `github_app` credentials with RFC-0020
(Q5); whether an App token can request Copilot review (Q9); per-call tokens
under #1688 (the expiry ceiling); manifest-flow App registration (Q11);
stored per-project clone PATs (Risk 5).

## Alternatives rejected

- **Pass `env=` at each call site** (`_default_runner`, `gh.py`, Job
  dispatch). More than 8 edits, and the MCP provider, settings and context
  code would still read `os.environ`. Q2 settled on one process-level change.
- **A machine user, or reusing `GITHUB_BOT_TOKEN`.** A machine user is type
  User and its approval would count (Q1). `GITHUB_BOT_TOKEN` is of unknown type
  and could let a User PAT in (Q6).
- **Overwrite a boot-time PAT instead of refusing to start.**
  `/proc/<pid>/environ` would still hold it.
- **Start anyway when the first mint fails on a 5xx or network error.** The
  server would run with no token, and `gh` would fall back to whatever is in
  `hosts.yml`. A crash loop during a GitHub outage costs little, because no
  PR work is possible then anyway.
- **Rely only on the `PRIVATE_KEY` deny pattern for the key.** It covers
  `child_env` spawns only; a bare `subprocess` call or a future helper that
  copies `os.environ` would still pass it on. Popping the key covers every
  spawn in one line.
- **Sleep until `expires_at` minus 30 minutes.** It is equivalent while tokens
  last 1 hour; a fixed interval is simpler to test.
- **Mint lazily on first use or on expiry.** That needs a hook in every
  consumer, which defeats the process-env approach.
- **A token cache class, a `GitHubIdentity` abstraction, or PyJWT/`githubkit`.**
  One identity, one interval: `os.environ` is the cache, and jose plus httpx
  are installed.
- **A sidecar or CronJob writing the token into a Secret.** A Secret change
  does not reach a running process's env, and it adds a component.
- **Refuse `human-approval` tasks when no App is configured.** It breaks
  backward compatibility (Q8, intent:49, 89-90).
- **Drop `GITHUB_KEEP` from `RUNNER_KEEP`** (`utils/subprocess_env.py` on
  `dev`) so non-Claude runners lose the token. `run.py` and the other runners
  read it for the in-build PR endgame (`spec/2026-10-09-1680-child-process-env.md`
  on `dev`, `RUNNER_KEEP`); taking it away breaks that path and belongs with
  #1688's per-call tokens.
- **A warning when `GITHUB_BOT_TOKEN` is set alongside the App.** Q6 leaves it
  alone and only the backend runner reads it.

## Risks

1. **Token expiry in long builds (accepted ceiling).** The in-pod build and the
   kubejob snapshot the token at dispatch (`build_backend.py:287-288`). Job
   deadlines run up to 6 h (`build_backend.py:184`), tokens last 1 h. A packed
   build that pushes after more than about 30-60 minutes
   (`apps/backend/cli/main.py:542` via `core/git_credentials.py:52`) fails
   that push, and the failure is best-effort and quiet
   (`workspace_fetch.py:89-106`). Marked with the `ponytail:` comment; upgrade
   path #1688. The server-side endgame always reads the fresh value. App-minted
   PRs still trigger Actions, so CI runs.
2. **Agents can still use the App token. This only partly meets the intent
   constraint "Agents never see the new credential"; review must accept it
   explicitly.** The private key never reaches an agent. The installation
   token does: non-Claude runners keep it through `RUNNER_KEEP` (on `dev`),
   and the GitHub MCP server gets it as `GITHUB_PERSONAL_ACCESS_TOKEN` because
   the probe reads `GITHUB_TOKEN`, then `GH_TOKEN` (`apps/backend/core/mcp_credentials.py:187-203`),
   as both get the PAT today. With it an agent can push, or approve as a Bot,
   which never clears the gate. A direct merge through the API is stopped only
   by branch protection. Strictly better than today, since none of it can
   produce a counting approval; #1688 closes it. Recorded in the matrix
   `claim`.
3. **PAT copies outside the process env.** Project `.env` files
   (`routes/github.py:129`, `projects.py:952`) and the UI settings token
   (`settings.py:2571`) may still hold a User PAT. The boot check covers the
   process env (including `apps/web-server/.env` via `env_bootstrap.py`) and
   `hosts.yml` only. The runbook tells the operator to remove them, and the
   matrix `claim` records it.
4. **GitHub Models stops working with the App on.**
   `apps/backend/providers/factory.py:196` uses `GITHUB_TOKEN` as the API key,
   and installation tokens probably lack `models:read`. Documented as
   unsupported with the App.
5. **Stored per-project clone PATs** (`project_workspace_service.py:238-285`)
   are User tokens the pod still holds. The endgame does not use them, but a
   counting approver is within reach. Out of the decided scope; recorded in the
   matrix `claim` with a follow-up.
6. **Remotes with a username.** Workspace clones put `oauth2@` in the URL for
   credentialed fetches (`_inject_credential`, `project_workspace_service.py:111-116`). It is unverified whether
   `gh auth git-credential` returns the token for a different username. If not,
   the push fails closed. The live test covers it.
7. **Refresh failure.** The old token stays and the loop retries every
   60 seconds; PRs fail only if GitHub is unreachable for the remaining
   30+ minutes of the token's life.
8. **The deny pattern is the only layer for a key that is never popped.** If
   `start()` returns early (no App configured) the key is absent anyway; if it
   raises, the server does not run. A test pins that `child_env` and
   `make_subprocess_env` drop `AIFACTORY_GITHUB_APP_PRIVATE_KEY`.
9. **Concurrent env writes.** A `putenv` from the event loop can race a `fork`
   in a worker thread once every 30 minutes; glibc locks the env, so the chance
   is very low. Noted, not fixed.
10. **Repos without the App installed.** Push and `gh pr create` fail with the
    existing warning (`pr_endgame.py:576`). The docs say so.
11. **Copilot review request may fail for App tokens.** Best-effort (Q9).

## Verification

**Unit tests, new `apps/web-server/tests/test_github_app.py`** (`monkeypatch`,
an RSA key generated with `cryptography`, `httpx.post` mocked):
- No App vars: `start()` does nothing and `configured()` is false.
- A partial set of App vars refuses to start.
- Each of `GITHUB_TOKEN`, `GH_TOKEN`, `GITHUB_PERSONAL_ACCESS_TOKEN`, a
  `tokenEnv`-named var, and a `hosts.yml` with a `github.com` token refuses to
  start.
- After `start()`: the key is gone from `os.environ`, both token names hold the
  minted value, and the JWT verifies against the public key with
  `iss == app_id` and `exp - iat <= 600`.
- A mint error at start raises.
- `refresh_loop`: a success replaces the token; a failure keeps the old one,
  never writes `""`, and retries after 60 s.

**Other tests:**
- `subprocess_env` test: `child_env()` and `make_subprocess_env()` both drop
  `AIFACTORY_GITHUB_APP_PRIVATE_KEY` (Risk 8).
- `apps/web-server/tests/test_pr_endgame_merge_gate.py` (`caplog`): the Q8 warning fires when
  human approval is required and no App is configured, and not otherwise.
- New `tests/helm/test_github_app_toggle.py`, same pattern as
  `tests/helm/test_saml_scim_toggle.py`: the default render has no
  `AIFACTORY_GITHUB_APP_*`; an enabled render has the three vars with the key
  only as a `secretKeyRef` and never in the Job template; enabled with
  `mcpCredentials.enabled` and `providers.github` fails with the #1671
  message; enabled with an empty `appId` fails.

**Commands:**
- `cd apps/web-server && pytest tests/test_github_app.py tests/test_pr_endgame*.py -q`
- From the repo root: `pytest tests/helm -q`
- `python scripts/gen_autonomy_matrix.py --check` (the required `autonomy matrix` check)

**Done (Q12), on AIFactory's own repo** with the App installed and the PAT
removed:
- Run one `human-approval` task. `gh api repos/:o/:r/pulls/N --jq .user.login`
  returns `<slug>[bot]`, and the last pusher is the bot (Risk 6 covered).
- The PR's required checks ran and passed (`gh pr checks N`), so CI
  triggers on App-authored PRs (intent constraint "CI still runs").
- The maintainer approves the head commit.
- The logs show `[pr-endgame] pr=N approved by <maintainer> at <sha>` followed
  by the merge, with no manual step.
- `kubectl exec ... -- sh -c 'tr "\0" "\n" </proc/1/environ | grep -c -E "^(GH_|GITHUB_)TOKEN=|^GITHUB_PERSONAL_ACCESS_TOKEN="'`
  returns 0 on the server pod, and the same check for
  `AIFACTORY_GITHUB_APP_PRIVATE_KEY` returns 0 on agent and Job pods.
