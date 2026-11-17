---
title: GitHub App identity — operator setup
sidebar_position: 7
---

# GitHub App identity — operator setup

By default AIFactory opens and merges PRs with a maintainer's token, so the
maintainer is the PR author and cannot approve their own PR. A task with a
`human-approval` system gate then has to be merged by hand. With a GitHub App,
the PR author is a bot, and a maintainer's approval of the head commit clears
the gate and merges the PR.

Unconfigured installs behave as before and log a warning when a task waits on a
human approval.

## 1. Register the App

### Register with the manifest (recommended)

Run `python scripts/github_app_setup.py manifest --out app.html` from the
repository root. Add `--org ORG` for an organization-owned App. Add
`--workflows` only if PRs touch `.github/workflows`. App names are unique on
GitHub; pass `--name NAME` if "AIFactory" is taken. `manifest` and `convert`
need only the Python standard library, and neither touches the cluster.

Open `app.html` and create the App. GitHub then redirects to
`http://localhost/?code=...`. That page does not load, which is expected: copy
the `code` value from the browser's address bar. The code works once and expires
after one hour. Then run:

```bash
python scripts/github_app_setup.py convert CODE --out secret.yaml
```

The command writes the private key into a Secret manifest (file mode 0600) and
prints the App ID, the App slug and the install URL.

The Secret is named `aifactory-github-app` with the key `private-key`. That
matches the Helm values below, so with Helm you can `kubectl apply -f
secret.yaml`. It does not match a deployment that reads the three
`AIFACTORY_GITHUB_APP_*` variables from a shared Secret, as the gitops
deployment does. For that, extract the raw PEM (the 4-space-indented lines):

```bash
(umask 077; sed -n 's/^    //p' secret.yaml > private-key.pem)
```

`private-key.pem` is as sensitive as the Secret. Delete both files once the key
is stored, and keep one copy in a password manager.

### Register manually

On GitHub, create an App with these repository permissions and no others:

- Contents: read and write
- Pull requests: read and write
- Issues: read and write
- Checks: read
- Metadata: read
- Workflows: read and write, only if your PRs touch `.github/workflows`
- Actions: read, only if the PR endgame has to read workflow runs

Do not grant Administration, Secrets, Organization or Members permissions. Generate a private key (PEM).

## 2. Install it

Install the App on every repo AIFactory opens PRs against. Choose "Only select
repositories" and pick the repos your AIFactory projects build on. A project
whose repo is not covered cannot push: it fails with the existing push or
`pr create` warning.

The installation ID is the number at the end of the installation settings URL
(`https://github.com/settings/installations/<ID>`). You can also read it from
`GET /app/installations`, called with an App JWT.

## 3. Configure AIFactory

Create a Secret holding the PEM, then set the chart values:

```yaml
githubApp:
  enabled: true
  appId: "12345"
  installationId: "67890"
  secretName: aifactory-github-app
  privateKeyKey: private-key
```

Outside Helm, set `AIFACTORY_GITHUB_APP_ID`, `AIFACTORY_GITHUB_APP_INSTALLATION_ID`
and `AIFACTORY_GITHUB_APP_PRIVATE_KEY`. All three or none; a partial set stops
the server at boot.

When the variables come from a shared Kubernetes Secret, load the three values
without printing them, from the PEM file made above:

```bash
jq -n --arg id "$APP_ID" --arg inst "$INSTALLATION_ID" --rawfile pem private-key.pem \
  '{stringData:{AIFACTORY_GITHUB_APP_ID:$id,
                AIFACTORY_GITHUB_APP_INSTALLATION_ID:$inst,
                AIFACTORY_GITHUB_APP_PRIVATE_KEY:$pem}}' |
  kubectl -n NAMESPACE patch secret SECRET_NAME --type merge --patch-file /dev/stdin
```

Also put the same three values into your declarative secret source (agenix,
sealed or external secrets), or the next rebuild drops them.

At boot the server mints an installation token, writes it to `GH_TOKEN` and
`GITHUB_TOKEN`, removes the private key from its environment and refreshes the
token every 30 minutes. It fails closed: if the first mint fails, the server
does not start.

## 4. Remove old PATs

The server refuses to start while a PAT is present, and the chart refuses to
render with the MCP GitHub PAT:

- Unset `GITHUB_TOKEN`, `GH_TOKEN`, `GITHUB_PERSONAL_ACCESS_TOKEN` and the
  variable named by `github.tokenEnv` in `mcp-credentials.json`. An empty value
  is fine.
- Set `mcpCredentials.providers.github=false`.
- Remove any `github.com` token from the `gh` `hosts.yml`.
- Remove PAT copies from project `.env` files and the UI settings token. The
  server does not clear these.

## Cutover order for the gitops deployment

The factory-gitops Deployment carries the three App variables as optional
secret references, so they stay unset until the keys exist. The server refuses
to start with a partial App set, with any PAT still present (`GITHUB_TOKEN`,
`GH_TOKEN`, `GITHUB_PERSONAL_ACCESS_TOKEN`, the `github.tokenEnv` alias) and
with a token in the `gh` `hosts.yml`. Do this in one window (full steps:
`docs/aifactory-github-app-cutover.md` in factory-gitops):

1. Create the App from the manifest and install it on the repos your projects
   build on.
2. Add all three keys to `factory-secrets` (and to your declarative secret
   source), then immediately merge the change that removes the Deployment's
   `GITHUB_TOKEN` entry. A restart between the two steps would not boot.
3. Check that no other PAT source is left (see "Remove old PATs"), that
   `AIFACTORY_AGENT_GITHUB_TOKEN` is unset (it would override the App token for
   children), and that `apps/backend/.env` holds no GitHub token.
4. Verify: the pod boots and its logs are clean, no PAT variable is set in the
   pod, and one small task opens a PR authored by `app/<slug>`.
5. Set `AIFACTORY_AGENT_TOKEN_ADMIN_POLICY=refuse`.
6. Reduce the old classic PAT's scopes (other services may still use it) to
   `repo`, `workflow`, `copilot` and `read:org`, with an expiry.

The server mints installation tokens (1 hour, refreshed every 30 minutes) and
exports `GH_TOKEN` and `GITHUB_TOKEN` inside its own process, so the `gh`
git-credential helper keeps working with no token in the pod's environment.

## Copilot review

AIFactory's own review engine reviews PRs by default (`AIFACTORY_PR_REVIEWER`
unset or `aifactory`). Copilot review is opt-in. Copilot gates a PR only when the
reviewer is `copilot` or `copilot-ruleset`. A missing Copilot review times out
after 20 minutes and stops for a human.

What was observed: on one deployment, `probe-copilot` run against a test PR
with an App installation token returned `HTTP 201; copilot_listed: False`. The
request was accepted, but GitHub did not list Copilot as a reviewer, so Copilot
was not requested through the App token there. A repository without Copilot
entitlement or a PR that Copilot already reviewed give the same answer, so
treat a negative result as strong evidence for your setup, not as proof. The
probe sends a real review request: run it only on a test PR, and before any
Copilot ruleset is active on that repository, or the ruleset makes it pass for
the wrong reason.

With `copilot`, the server asks GitHub to add Copilot as a reviewer and checks
that the answer lists Copilot. If the request fails or Copilot is not listed,
the server logs one warning and keeps waiting.

With `copilot-ruleset`, the server sends no request and a repository ruleset
must. This is the way to use Copilot with an App token. To opt in a project:

1. In Project settings, set the project's reviewer to `copilot-ruleset`. The
   instance-wide default is `AIFACTORY_PR_REVIEWER`; leave it unset to keep
   `aifactory` for every other project.
2. On that repository, create the ruleset (or set an existing one to
   `enforcement=active`). The rule type is `copilot_code_review`; the values
   below review on open, not on every push, and skip drafts:

   ```bash
   gh api -X POST repos/OWNER/REPO/rulesets --input - <<'JSON'
   {"name": "Copilot code review", "target": "branch", "enforcement": "active",
    "conditions": {"ref_name": {"include": ["~ALL"], "exclude": []}},
    "rules": [{"type": "copilot_code_review",
               "parameters": {"review_on_push": false,
                              "review_draft_pull_requests": false}}]}
   JSON
   # enable an existing ruleset:
   gh api -X PUT repos/OWNER/REPO/rulesets/ID -f enforcement=active
   ```

A repository without the ruleset never gets a Copilot review, so a project on
`copilot-ruleset` there stops for a human after 20 minutes. Keep such projects
on `aifactory`. Set `review_on_push` to `true` to have fix cycles reviewed
again; that option was not tested here.

To run the probe:

```bash
python scripts/github_app_setup.py probe-copilot --repo OWNER/NAME --pr N --app-id ID --installation-id ID --key-file private-key.pem
```

It needs `python-jose` and `httpx` (the backend virtualenv has both), never
prints the token or key, and exits 0 only when GitHub lists Copilot as a
reviewer. Record the result on #1701.

## Clone credentials

Stored per-project clone PATs are an accepted risk. A PAT is read at project
add for the clone through `GIT_ASKPASS`, stays encrypted at rest, and never
pushes.

To move a project off its PAT, install the App on its repo and add the project
with no stored credential. Then delete the stored PAT through the UI or
`DELETE /api/git-credentials/{id}`, and revoke it on GitHub. Keep a PAT only
where the App cannot be installed.

## Token for agent children

Runner children and build Jobs use GitHub for the base-branch fetch, the
packed-workspace branch push, PR review reads and the GitHub MCP tools. The PR
create, review request and merge calls run in the web server and keep its own
token.

Without a dedicated token, the children that need GitHub share the control
plane's token. To give them a narrower one, create a fine-grained PAT and put
it in a Secret:

- Repositories: only the ones AIFactory builds.
- Permissions: Contents read and write, Pull requests read, Issues read,
  Metadata read.

```bash
kubectl create secret generic agent-github-token --from-literal=token=<PAT>
```

```yaml
agentGithubToken:
  secretName: agent-github-token
  secretKey: token
```

The chart sets `AIFACTORY_AGENT_GITHUB_TOKEN` from this Secret. The web server
passes it to the children as `GITHUB_TOKEN`, `GH_TOKEN` and
`GITHUB_PERSONAL_ACCESS_TOKEN`. They never see the control plane's GitHub
token, and never see the variable itself.

Admin guard (#1973). Whatever token the children would get, a classic PAT or
OAuth token whose scopes include `delete_repo` or an `admin:*` scope is
checked once an hour against `GET /user`. With
`AIFACTORY_AGENT_TOKEN_ADMIN_POLICY=warn` (the default) the server logs the
scope names once per token and still passes it. With `refuse`, or any value
other than `warn`, the children get no GitHub token and the `.env` loaders skip
GitHub names, so a refused token cannot come back. An unreadable answer counts
as a finding under `refuse`. App installation, fine-grained and GitHub App user
tokens have no scopes to read and are not checked. The classic `repo` scope
still includes repository admin on repos the token owner administers; the guard
is a safety net, and the App is the fix. The control plane's own git and `gh`
calls and a per-project token saved by a tenant are not changed.

Notes:

- An empty Secret value counts as "not set". The server logs a warning and
  the children fall back to the control plane's token.
- The agent can read the children's token whenever the GitHub MCP server is
  on. Keep the PAT narrow.
- Do not point `github.tokenEnv` at this variable. The App start refuses it
  as a PAT.
- Do not put GitHub tokens in `apps/backend/.env`. While this token is set,
  the web server skips them when it builds a child's env, and it sets every
  GitHub name the children read (including a `github.tokenEnv` alias) to the
  narrow token, so a value in that file cannot replace it.
- The `github-models` provider reads its key from `GITHUB_TOKEN`. With a
  narrow token it needs the Models permission on that PAT, or another
  provider.

## Several installations

By default one installation id serves every org. To give each org its own
installation of the same App, the operator sets a map of org id to
installation id:

```yaml
githubApp:
  orgInstallations:
    default: "9001"
    acme: "9002"
```

The chart sets `AIFACTORY_GITHUB_APP_ORG_INSTALLATIONS=acme=9002,default=9001`.
Write the ids as quoted strings; the chart rejects numbers (use `--set-string`
on the command line). Only the operator sets the map. An installation id is
not a secret, so tenants must not be able to choose one.

A non-empty map turns on multi-installation mode, which fails closed:

- No process-wide token is published. A call gets a GitHub token only when it
  names a bound org. Bind every org, `default` included: an unbound org gets
  no GitHub token.
- A project path registered under two orgs (one inside the other) gets no
  token. In this mode the server refuses (409) to add or repoint a project
  whose path equals, holds or sits inside another org's project.
- It cannot be combined with the token for agent children
  (`agentGithubToken`); the chart and the boot check refuse both.
- Wired: project clone and the Auto-Fix pull, build, spec and PR-review
  runners, build Jobs, the PR endgame, the merger, the PR data service,
  Copilot dispatch, task-branch git calls, the shared `gh` helper, the
  control plane's worktree fetch for build Jobs, and the project routes of
  the GitHub and git APIs (`/api/projects/{projectId}/github/...`, the PR and
  git routes). The org comes from the project the call works on. Those routes
  refuse a caller who has no access to the project (403), and a handler cannot
  switch to another org's token.
- Still no token in this mode, by design: routes with no project (web login,
  `auth/status`, user, orgs, models), Kubernetes spec Jobs (they never carry
  a GitHub token), and a path no org owns. The server never stores an
  installation token in a project's settings (`/persist-token` refuses).
- If GitHub refuses to mint a token, the call runs without one and fails with
  an authentication error; the server log has the cause.
- A runner's token is fixed at spawn and lasts at least 55 minutes.

Rollback: empty the map (`githubApp.orgInstallations: {}`) and restart.

## Limits

- Per-org installations: see Several installations.
- GitHub Models is not supported with the App.
- The token is process-wide with a 1 hour lifetime, refreshed every 30
  minutes. A build or Job gets at least 30 minutes at spawn, so a long packed
  build can fail its final push. Per-call tokens (#1688) remove this.
- An agent can read the GitHub token of its runner whenever the GitHub MCP
  server is enabled (it is passed to the agent CLI as
  `GITHUB_PERSONAL_ACCESS_TOKEN`). Set a token for agent children to keep that
  narrow.
- Stored per-project clone PATs are an accepted risk; see Clone credentials.

## Rollback

In one `helm upgrade`, set `githubApp.enabled=false`, then restore the PAT
(`mcpCredentials.providers.github=true` or `GITHUB_TOKEN`). With the App still on,
the chart and the boot check refuse the PAT. To revoke fast, suspend or uninstall
the App; on a suspected key leak delete the key in the App settings and create a
new Secret.
