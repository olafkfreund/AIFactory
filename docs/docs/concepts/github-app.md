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

Open `app.html` and create the App. Copy the `code` value from the redirect URL
within one hour, then run:

```bash
python scripts/github_app_setup.py convert CODE --out secret.yaml
```

The command prints the App ID and install URL. Install the App on the repo,
then read the installation ID from the number at the end of its settings URL.
The operator applies the Secret with `kubectl apply -f secret.yaml`, then
deletes the local file. Keep the App's private key secure.

### Register manually

On GitHub, create an App with these repository permissions and no others:

- Contents: read and write
- Pull requests: read and write
- Metadata: read
- Workflows: read and write, only if your PRs touch `.github/workflows`

Do not grant administration permissions. Generate a private key (PEM).

## 2. Install it

Install the App on every repo AIFactory opens PRs against. A repo without the
App fails with the existing push or `pr create` warning.

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

## Copilot review

AIFactory's engine reviews PRs by default. Copilot gates a PR only when
`AIFACTORY_PR_REVIEWER=copilot`. A missing Copilot review times out to a human
stop.

To check whether the App can request Copilot review, run this probe on a test
PR opened by the App. Use the App's PEM private key file. The probe needs
`python-jose` and `httpx` (the backend virtualenv has both) and never prints
the token or key; it exits 0 only when GitHub lists Copilot as a reviewer:

```bash
python scripts/github_app_setup.py probe-copilot --repo OWNER/NAME --pr N --app-id ID --installation-id ID --key-file private-key.pem
```

Record the result on #1701. If the request is refused, add a branch ruleset
with "Automatically request Copilot code review".

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

## Limits

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
