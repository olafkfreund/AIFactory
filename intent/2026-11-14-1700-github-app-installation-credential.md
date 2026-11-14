---
status: approved
issue: 1700
author: olafkfreund
---

# Intent: per-tenant GitHub App installations

## Problem

AIFactory can act as one GitHub App installation, set deployment-wide through
Helm values (#1671, `apps/web-server/server/services/github_app.py`). The App
authentication path cannot select another installation: the server mints one
token for one installation id and publishes it process-wide. A tenant whose
repositories sit under a different installation of the same App can only
store a PAT, and that PAT is used for the first clone alone
(`routes/projects.py:576`); later pushes and PRs still use the process-wide
token, which does not cover that tenant's repos.

The stored-credential kind `github_app` is reserved for this
(`database/models.py:452`); the API rejects it
(`routes/git_credentials.py:144`) and the clone path ignores it
(`routes/projects.py:358`).

The owner decided the shape on 2026-10-17 and 2026-10-31: one
deployment-wide App key, plus a non-secret `installation_id` per tenant, with
tokens minted per call. Not one App key per tenant. The owner also deferred
the issue until a second tenant needs it. For the 2026-11-14 bulk run the
instruction is to build the path behind configuration and test it with
mocks, because the App has not been created yet (owner comment on #1973,
2026-10-31) and nothing can be tried live.

## Proposed outcome

- The operator can bind a tenant (org) to an App installation. Projects of
  that org are cloned, fetched, pushed and PR'd with tokens minted for that
  installation.
- Only the operator can create that binding. An installation id is not a
  secret, and the server holds the one App key, so a tenant who could name
  an id could act as another tenant's installation. A binding that is
  malformed fails closed at start.
- A minted per-tenant token is used for the call it was minted for. It is
  never published process-wide, where a concurrent request for another
  tenant would pick it up.
- With no bindings configured, nothing changes.
- The path is inactive unless the App is configured, and is covered by tests
  with a mocked GitHub API. When a second tenant appears, the work left is:
  install the App for them and add the binding.

## Affected users and systems

- AIFactory web-server: `services/github_app.py`, the project clone, the
  runner and Job spawn env, the PR endgame; `apps/backend/core/git_credentials.py`
  (where the web server's git and `gh` calls get their token).
- The Helm chart and `docs/docs/concepts/github-app.md`.
- Operators. Tenants see no new API. Existing `pat` credentials are
  unchanged.

## Constraints

- Follows the owner's decision: per-tenant installation id, one App key. No
  per-tenant private keys and no new secret storage (Factory RFC-0020 Phase 3
  is still Proposed).
- Tenant isolation: a tenant must not be able to choose, change or learn to
  use another tenant's installation. Nothing a tenant can edit (project
  settings, stored credentials, request bodies) selects the installation.
- An installation id is validated as a number before it is used in a GitHub
  API URL.
- A minted token is only ever offered to `https://github.com/` remotes.
- Minted tokens never reach logs or argv. In a runner or Job they are
  handled exactly like the token those children get today (#1973).
- Builds on the child-token change for #1973; no other env scrubbing (#1931).

## Open questions

None. The issue stays open and deferred after this lands; the owner closes
it when a real second installation has been exercised.

Note added with the spec (2026-11-14): the spec is stricter than the limit
first written here. Once any binding exists, an org without one gets no
GitHub token instead of the default installation's.
