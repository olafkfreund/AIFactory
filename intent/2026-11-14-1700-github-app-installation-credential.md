---
status: draft
issue: 1700
author: olafkfreund
---

# Intent: per-tenant GitHub App installation credentials

## Problem

AIFactory can act as one GitHub App installation, set deployment-wide through
Helm values (#1671, `apps/web-server/server/services/github_app.py`). A tenant
whose repositories sit under a different installation of the same App cannot
be served: every clone, push and PR uses the one process-wide token.

The stored-credential kind `github_app` is reserved for this
(`database/models.py:452`) but the API rejects it
(`routes/git_credentials.py:144`) and the clone path ignores it
(`routes/projects.py:358`). So a tenant on another installation has to store
a long-lived PAT instead.

The owner decided the shape on 2026-10-17 and 2026-10-31: one
deployment-wide App key, plus a non-secret `installation_id` per tenant, with
tokens minted per call. Not one App key per tenant. The issue is deferred
until a second tenant needs it, and the App itself does not exist yet, so
none of this can be tried live.

## Proposed outcome

- An org can store a `github_app` credential that holds only an installation
  id. No token or key is stored for it.
- A project added with that credential is cloned with a token minted for
  that installation, and its later GitHub work (base fetch, branch push, PR
  create, review request, merge) uses tokens for the same installation.
- Projects without such a credential behave exactly as today.
- The whole path is inactive unless the App is configured, and is covered by
  tests with a mocked GitHub API. When a second tenant appears, the work left
  is: install the App for them and store the installation id.

## Affected users and systems

- AIFactory web-server: `services/github_app.py`, `routes/git_credentials.py`,
  `routes/projects.py`, the runner and Job spawn env, the PR endgame.
- Docs: `docs/docs/concepts/github-app.md`.
- Org admins who store Git credentials. Existing `pat` credentials are
  unchanged.

## Constraints

- Follows the owner's decision: per-tenant installation id, one App key.
- No per-tenant private keys, and no new secret storage (Factory RFC-0020
  Phase 3 is still Proposed).
- No database migration if the existing columns can hold it.
- A tenant must not be able to use another tenant's installation: the id is
  bound through the org-scoped credential, never taken from a request at
  dispatch time.
- Minted tokens never reach logs, argv or the agent's env.
- Builds on the child-token change for #1973; no other env scrubbing (#1931).

## Open questions

None. The issue stays open after this lands: it closes when a real second
installation has been exercised.
