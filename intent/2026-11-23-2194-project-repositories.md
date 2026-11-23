---
status: draft
issue: 2194
author: olafkfreund
parent: olafkfreund/Factory#3857 (phase 1b slice)
---

# Intent: projects hold many repos, and services can read a tenant's registry

## Problem

The #3857 owner update (2026-11-19) defines a project as a container that
holds 1..n git repositories (GitHub, GitLab, Azure DevOps). That was phase
1b. It was skipped: phases 4 and 2 (P3) shipped on top of the phase 1
registry. That registry still has one nullable, non-unique
`repo_identity` per project (`database/models.py:328-330`, "never unique:
recorded, not used to merge"). It drops URL ports and records neither a
provider nor a default branch (`project_identity.py:34-55`).

Owner decision 2026-11-23: build this slice before CFactory#574 (cards
target a project and one of its repos). Today:

- **One repo per project.** A project cannot hold a GitLab service repo
  and a GitHub docs repo together.
- **No way to give a service a tenant-wide read.**
  - P3 grants an ownerless scoped principal only its bound org's
    default-team projects (`project_authz.py:96-101`).
  - Ownerless keys cannot be minted at all: `ApiKey.user_id` is NOT NULL
    (`models.py:476`), and the mint route always sets the current user
    (`api_keys.py:216-224`).
  - A user key needs `api` for REST (`mcp_remote/auth.py:129`), which
    raises it to member everywhere.
  - The legacy shared token sees every org's active projects (subject to
    the tenant-stamp filter), or none, depending on configuration.
  - `project_to_response` has no org id and no archive marker
    (`routes/projects.py:396-407`). It includes local paths and settings
    that a service does not need.
  - Successful key use on REST is not audited (`auth.py:173-188`).

## Proposed outcome

1. **Projects hold repos.** Each project has 0..n repos. Each repo has:
   - a stable id;
   - provider: `github`, `gitlab` or `azure_devops`, the shared Factory
     provider names;
   - canonical instance: web host, port and path prefix, so GitHub
     Enterprise API and web URLs and path-prefixed self-hosted GitLab map
     to one key;
   - full namespace (GitLab subgroups included);
   - default branch;
   - role: `primary`, `service`, `lib`, `docs` or `infra`.

   When a project has repos, exactly one is primary. Removing the primary
   repo needs another to be promoted first. A project with no remote stays
   valid with zero repos.
2. **Uniqueness is per org.** A repo belongs to at most one active project
   within an org, keyed on `(org, canonical instance, namespace)`.
   - Two orgs may track the same repo.
   - A conflict never reveals another org's project.
   - Archiving a project keeps its repo rows readable and releases the
     claim. Reactivating it fails with a conflict if another active project
     now holds the repo.
3. **The migration is safe on existing data.**
   - Each project's `repo_identity` becomes its primary repo, with provider
     and default branch resolved where the data allows.
   - Where provider or default branch cannot be resolved, they are marked
     `unresolved` for later fixing. Nothing is guessed.
   - Existing duplicates within an org are reported by a preflight and left
     linked to the project that is oldest by creation date, with the others
     flagged `conflict`. Ids, slugs and history are kept.
   - `POST /api/projects` creates the primary repo row from now on.
4. **Repos can be managed.**
   - Role-checked routes list, add, remove, set primary and update role or
     default branch.
   - Project responses include the repos.
   - `repoIdentity` stays as the primary repo's identity through phase 3.
   - The AIFactory project page gets a Repositories list where admins can
     add, remove and mark the primary.
5. **A tenant registry read for services.**
   - A new service key type is minted by an org admin. It is ownerless and
     bound to one org, carries the dedicated scope `registry:read`, and can
     be revoked.
   - `registry:read` grants no project-route access: it is not in the P3
     scope ceiling.
   - It reads `GET /api/orgs/{org_id}/registry`: every project of that org,
     all teams, archived ones on request. Each project carries `id`,
     `name`, `slug`, `team_id`, `org_id`, `archived` and its repos. Local
     paths and settings are left out.
   - A path org different from the key's org, an unknown org, the legacy
     shared token and dev-bypass principals all get the identical 404.
   - With multi-tenant on, the org-to-tenant-stamp mapping is enforced.
   - Every read and every repo change writes an audit event (key or actor
     id, org, project and repo ids, outcome).

## Affected users and systems

- **AIFactory:**
  - DB: new `project_repositories` table, a nullable `api_keys.user_id`
    for service keys, migrations;
  - `project_store`, `project_identity`, `routes/projects.py`,
    `routes/api_keys.py` (service-key mint and revoke), `project_authz`,
    the audit service, and the project page UI.
- **CFactory#574 and CFactory#575** consume the registry read. CFactory
  stores one service key per tenant and maps tenant to org explicitly,
  failing closed.
- **PFactory, TFactory and the factory CLI** are unchanged in this slice.

## Constraints

- **No lockout or behaviour change** for existing users, keys and portals:
  - project ids, slugs, `/p/<slug>`, P3 checks and `repoIdentity` stay as
    they are;
  - the migration is additive, and rollback keeps repo links in an export.
- **D6 on every new route.** Unknown and invisible projects get the
  identical `404 Project not found`; a visible project with too low a role
  gets 403. Access is checked before repo lookup or conflict handling.
- **Least privilege.** A service key reads only the registry, of only its
  org. Minting needs org admin.
- **Out of scope**, listed so it is not lost:
  - boards and Jira;
  - per-project settings documents;
  - per-portal checkout settings;
  - per-repo enabled portals;
  - validating repo access with stored credentials on add, which CFactory
    checks when it makes a repo selectable in #574;
  - repo targeting for AIFactory tasks, PFactory plans and TFactory runs;
  - other portals reading the registry (phase 3).

## Open questions

1. **Uniqueness scope.** Per org (above) versus global `(host, namespace)`
   from the owner update. Default: per org, so tenants stay isolated and a
   conflict cannot leak another org.
2. **Who may call the registry read.** Default: service keys only. Users
   keep `GET /api/projects`.
3. **Duplicate resolution in the migration.** Default: the oldest project
   keeps the link, the others are flagged `conflict`, and the admin UI
   shows them for fixing.
