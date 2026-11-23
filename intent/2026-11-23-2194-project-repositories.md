---
status: draft
issue: 2194
author: olafkfreund
parent: olafkfreund/Factory#3857 (phase 1b slice)
---

# Intent: projects hold many repos, and services can read a tenant's registry

## Problem

The #3857 owner update (2026-11-19) defines a project as a container that
holds 1..n git repositories, on GitHub, GitLab or Azure DevOps, one of them
the primary. That was phase 1b. It was skipped: phases 4 (URL project view)
and 2 (project enforcement, P3) shipped on top of the phase 1 registry,
which still has a single `repo_identity` string per project
(`database/models.py:308`, `project_identity.py`).

Owner decision 2026-11-23: build the 1b slice before CFactory#574 (cards
target a project and one of its repos). Today:

- **One repo per project.** A project cannot say "this GitLab service repo
  and this GitHub docs repo are the same project". `repo_identity` is
  recorded but "never unique: recorded, not used to merge". It has no
  provider, no namespace split and no default branch.
- **Services cannot read one tenant's registry safely.**
  - `GET /api/projects` filters by the caller's grants.
  - An org-scoped service key (no user) only sees default-team projects
    (`project_authz.py:96-101`).
  - The legacy shared token returns every org's projects mixed together,
    or nothing at all, depending on configuration.
  - The response has no org id and no archived marker
    (`routes/projects.py:396-407`).
  - CFactory therefore cannot build its project list from the registry
    without either missing projects or seeing another org's.

## Proposed outcome

1. **Projects hold many repos.**
   - A project has a list of repositories. Each repo has:
     - provider (`github`, `gitlab` or `azure_devops`);
     - web host, and port if any;
     - full namespace (GitLab subgroups included);
     - default branch;
     - role (`primary`, `service`, `lib`, `docs` or `infra`).
   - Exactly one repo is primary.
   - A repo belongs to at most one active project, by normalized
     `(host, namespace)`.
   - Existing projects are migrated to one primary repo from their
     `repo_identity`. Projects with no remote get no repo and stay valid.
2. **Repos can be managed.**
   - Role-checked routes list a project's repos, add one, remove one and
     set the primary.
   - Adding a repo that another active project already holds returns a
     clear conflict.
   - Project responses include the repo list.
   - `repo_identity` stays readable as the primary repo's identity until
     the other portals stop using it.
3. **A tenant-scoped registry read for services.**
   - A service caller bound to one org (scoped key with an org binding and
     a read scope) can list every project of that org, all teams included,
     with org id, slug, repos and an archived flag (archived ones only when
     asked).
   - It never returns another org's projects. The legacy shared token gets
     no special widening on this read.
4. **The UI shows the repos.**
   - The AIFactory project page lists a project's repos and lets an admin
     add, remove and mark the primary.
   - The project switcher and `/p/<slug>` behave as today.

## Affected users and systems

- **AIFactory:**
  - DB: new `project_repositories` table, migration;
  - `project_store`, `project_identity`, `routes/projects.py`,
    `project_authz` (the service read), the project settings UI.
- **CFactory#574 and CFactory#575** consume the service read and the repo
  list.
- **PFactory, TFactory and the factory CLI:** unaffected in this slice.
  They keep their own lists until phase 3, and `repo_identity` stays
  available.

## Constraints

- **No lockout or behaviour change** for existing users, keys and
  portals:
  - project ids, slugs, `/p/<slug>` and P3 checks are unchanged;
  - the migration is additive and reversible.
- **Identical 404 for unknown or invisible projects** (D6) on every new
  route.
- **The service read is tenant-bound and least-privilege:**
  - it is a read scope only;
  - org binding is required;
  - it is not available to the legacy shared token;
  - it is audited like other key use.
- **Provider-neutral identity.** GitHub Enterprise (`/api/v3` base URLs),
  GitLab subgroups, ports and path-prefixed self-hosted GitLab all
  normalize to one stable `(host, namespace)` key.
- **Out of scope:**
  - boards (Jira and others);
  - per-project settings documents;
  - per-portal checkout settings;
  - other portals reading the registry (phase 3);
  - cross-repo tasks.

## Open questions

1. **Service read shape.** Option A: a new route,
   `GET /api/orgs/{org_id}/projects` for org-bound service keys. Option B:
   widening `GET /api/projects` for scoped service keys to all teams.
   Default: A, which leaves the user-facing list untouched.
2. **Which scope the CFactory key needs.** Default: the existing
   `project:read`, plus a key minted org-bound for CFactory's tenant. One
   key per CFactory tenant, stored in CFactory's
   `aifactory_org_tokens`.
3. **What happens to `repo_identity` long term.** Default: keep it as a
   derived field (the primary repo) through phase 3, then deprecate it.
