---
status: draft
issue: 2194
intent: intent/2026-11-23-2194-project-repositories.md
---

# Spec: projects hold many repos; service registry read

Paths are relative to `apps/web-server/server/` unless stated, read on
`origin/dev` on 2026-11-23. The Alembic head is `9c4e2a6d1b83`.

## Design

### D1. Repository identity: new module `repo_ref.py`

`parse_repo_ref(url_or_ref, *, provider_hint=None) -> RepoRef | None`
returns these fields:
- `provider` ∈ {`github`, `gitlab`, `azure_devops`, `unresolved`}. These
  are the Factory `ProviderType` values.
- `instance`: lowercased host, plus `:port` when it is not the scheme
  default.
- `namespace`: the full path without a leading or trailing `/` or `.git`,
  case preserved.
- `match_key`: `instance + "/" + namespace.lower()`.

Accepted forms:
- `https://`, `http://`, `ssh://` and `git://` URLs;
- scp-like `git@host:path`;
- bare `host/path`.

Rules:
- **GitHub API hosts.** `api.github.com` maps to `github.com`. An
  `https://<host>/api/v3/repos/<o>/<r>` URL maps to `<host>` and `<o>/<r>`.
- **Provider detection:**
  - `github.com` gives `github`; `gitlab.com` gives `gitlab`;
  - `dev.azure.com` and `*.visualstudio.com` give `azure_devops`, with the
    `_git` segment stripped;
  - otherwise `provider_hint` (the project settings `gitProvider`);
  - otherwise `unresolved`.
- **GitLab path prefixes** are kept in `namespace` (e.g.
  `gitlab/group/sub/repo` for `https://corp/gitlab/group/sub/repo`).
  Consumers match a connection's web path prefix against the start of
  `namespace`, so no prefix knowledge is needed here.
- **Rejected inputs:** local paths, `file://`, and URLs with userinfo.
  `user@` is allowed only in the scp/ssh form.

`project_identity.normalize_repo_identity` is unchanged, so `repoIdentity`
stays byte-identical for current readers.

### D2. Table `project_repositories`

Migration `20261123_<rev>_project_repositories.py`, with `down_revision`
`9c4e2a6d1b83`. It follows the project_identity migration style:
`batch_alter_table`, named constraints, nullable additions.

Columns:

| Column | Type | Notes |
|---|---|---|
| `id` | String(36), uuid pk | |
| `project_id` | FK projects | not null, indexed |
| `org_id` | FK organizations | not null |
| `provider` | String(16) | not null |
| `instance` | String(255) | not null |
| `namespace` | String(512) | not null |
| `match_key` | String(768) | not null |
| `default_branch` | String(255) | nullable |
| `role` | String(16) | `primary`, `service`, `lib`, `docs` or `infra`; not null, default `service` |
| `status` | String(16) | `ok`, `unresolved` or `conflict` |
| `active_claim` | String(768) | nullable; `match_key` when the project is active and `status != conflict`, else NULL |
| `primary_for` | String(36) | nullable; `project_id` when this repo is the primary, else NULL |
| `created_at`, `updated_at` | | |
| `created_by` | FK users | nullable |

Constraints:
- `uq_project_repos_org_claim` unique `(org_id, active_claim)`.
  Because NULLs never collide, this enforces "one active project per repo
  per org" on SQLite and Postgres alike.
- `uq_project_repos_primary` unique `(primary_for)`: at most one primary
  per project.
- `uq_project_repos_project_match` unique `(project_id, match_key)`.

Rules, enforced in `project_store` inside one transaction:
- **Archive** (`_archive`) sets `active_claim = NULL` on the project's
  repos.
- **Reactivate** restores `active_claim`. A unique violation there means
  the repo is now held by another project, so the repo is set to
  `status = conflict` and stays readable. Reactivation itself still
  succeeds.
- **Primary invariant:**
  - when a project has 1 or more non-conflict repos, exactly one is
    primary;
  - the first repo added becomes primary;
  - deleting the primary while others exist returns 409 "promote another
    repository first";
  - deleting the last repo is allowed.

### D3. Backfill (runtime, idempotent)

There is no data migration; this follows the repo's runtime-fill pattern
(`project_store._upsert`). Function `backfill_repositories()` in
`project_store`:

- **When it runs:** at app startup, after `configure()`. On Postgres it
  holds an advisory lock; on SQLite it relies on the single writer.
- **Order:** active projects without repo rows, oldest `created_at`
  first, then archived ones.
- **Input:** `repo_identity`, else `settings.clonedFrom`, parsed with
  `parse_repo_ref(provider_hint=settings.gitProvider)`.
- **Result:**
  - the repo row is inserted as primary;
  - `default_branch` is set from `settings.clonedBranch` only when the
    project was created in clone mode, else NULL;
  - `status` is `ok`, or `unresolved` when the provider is unresolved.
- **Conflicts:** a claim conflict inserts the row as `status = conflict`
  with no claim and no primary, and logs it.
- **Report:** `GET /api/orgs/{org_id}/registry/conflicts` (org admin)
  lists conflicts and unresolved rows.
- **Repeat runs:** a project already holding repo rows is skipped.

From now on, `POST /api/projects` (`routes/projects.py:716-872`) creates
the primary row in the same save. A claim conflict there gives
`409 {"detail": "Repository already belongs to another project in this organization"}`.
Only same-org projects are ever compared.

### D4. Repository routes

All under `/api/projects/{project_id}/repositories` in `routes/projects.py`.
P3 resolution comes first (`require_project_access`), so unknown and
invisible projects get the identical `404 Project not found` and too low a
role gets 403. Repo lookups run only after the project is authorized.

| Method and path | Role | Body / effect |
|---|---|---|
| `GET` | viewer | list |
| `POST` | admin | `{url, role?, default_branch?, provider?}` → parse, claim; 422 if unparseable, 409 if same-org conflict |
| `PATCH /{repo_id}` | admin | `{role?, default_branch?, provider?}`; setting `provider` resolves an `unresolved` row |
| `POST /{repo_id}:primary` | admin | makes it the primary |
| `DELETE /{repo_id}` | admin | per the D2 rules |

A `repo_id` that is not under that project returns 404.

`project_to_response` gains `repositories: [{id, provider, instance,
namespace, defaultBranch, role, primary, status}]`. `repoIdentity` stays
as it is.

### D5. Service keys

- **Migration:** `api_keys.user_id` becomes nullable, and two columns are
  added: `created_by` (FK users, nullable) and `tenant_id` (String(64),
  nullable).
- **Mint, list and revoke:** `routes/organizations.py`,
  `POST /api/orgs/{org_id}/service-keys`, with
  `GET /api/orgs/{org_id}/service-keys` and
  `DELETE /api/orgs/{org_id}/service-keys/{key_id}`.
  - Callers need `require_org_role("admin")`, and must be human users:
    key callers are refused.
  - Body: `{name, scopes, expires_in_days 1..365}`. Scopes must be a
    subset of `SERVICE_SCOPES = {"registry:read"}`; anything else gives
    422.
  - The new row has `user_id = None`, `created_by` set to the caller,
    `org_id` from the path, and `tenant_id = resolve_tenant(request)` when
    multi-tenant is on.
  - The raw key is returned once, in the existing `acw_` format.
  - List shows the preview, name, scopes, created_by, expiry and last
    used. Revoke hard-deletes, matching user keys.
- **User key routes** (`/api/keys`) are unchanged. They already filter by
  `user_id` and never see service keys.
- **Principal handling.** `_acw_principal` already yields
  `scoped_service` for `user_id is None`. Ownerless keys already skip the
  `api` rule in `authenticate_rest`. Two additions make sure a
  `registry:read` key reaches nothing else:
  1. `routes/project_authz._team_grant`: the scoped-service branch
     (96-101) applies only when the key's scopes reach a project role
     (`scope_ceiling(scopes) is not None`). `registry:read` gives `None`,
     so there is no default-team admin grant.
  2. A test sweeps every route in the app table
     (`test_project_authz_route_table.py` pattern). A `registry:read` key
     must get 401, 403 or 404 everywhere except the registry read.
- **Last used.** `last_used_at` is updated on successful service-key
  use, throttled to at most once a minute.

### D6. Registry read

`GET /api/orgs/{org_id}/registry?include_archived=false`, in
`routes/organizations.py` (or a new `routes/registry.py` mounted at the
same prefix).

- **Allowed caller:** an ownerless key with `registry:read` whose `org_id`
  equals the path org. Every other caller gets the identical
  `404 {"detail": "Not found"}`: users, user keys, the legacy shared token
  (whatever `LEGACY_SHARED_TOKEN_FULL_ACCESS` says), dev bypass, a
  different org, or an unknown org. This is unlike `require_org_role`'s
  403.
- **Multi-tenant.** When it is on, results are limited to projects whose
  `settings.tenant_id` equals the key's `tenant_id`. A key with no
  `tenant_id` gets 404.
- **Response:**
  ```json
  {"org_id": "...", "projects": [{"id": "...", "name": "...",
   "slug": "...", "team_id": "...", "archived": false,
   "repositories": [{"id": "...", "provider": "...", "instance": "...",
    "namespace": "...", "default_branch": null, "role": "primary",
    "primary": true, "status": "ok"}]}]}
  ```
  It never includes `path`, `settings` or credentials. Ordering is by
  slug.
- **Errors** carry no detail beyond the fixed messages.

### D7. Audit

All events go through `services/audit_service.log_audit_event`, in the
request's DB session.

| Action | When |
|---|---|
| `registry.read` | each registry read: key id, org, project count |
| `service_key.create`, `service_key.revoke` | mint and revoke: actor, key id, scopes |
| `project_repo.add`, `.update`, `.primary`, `.remove` | repo changes: actor, project id, repo id, `match_key` |
| `project_repo.conflict` | backfill or reactivation conflicts |

The existing unused `ACTION_API_KEY_*` and `ACTION_PROJECT_*` constants
stay as they are.

### D8. Frontend (`apps/frontend-web/src/`)

- **Project settings.**
  - New section `repositories`: add it to `ProjectSettingsSection`, to
    `SectionRouter`'s switch and to the `AppSettings`
    `projectNavItemsConfig`.
  - Component `components/project-settings/RepositoriesSection.tsx`:
    - a list with provider, `instance/namespace`, role, default branch,
      primary and status badges;
    - admins can add by URL, set primary, change role or branch, resolve
      the provider of `unresolved` rows, and remove.
- **Org settings.** A Service keys section: create (name, scopes fixed to
  `registry:read`, expiry), list and revoke. The raw key is shown once
  with a copy button.
- **Plumbing.** `lib/api-adapter.ts` calls, `shared/types/project.ts`
  `ProjectRepository`, and vitest tests for both sections.

## Alternatives rejected

- **Global `(host, namespace)` uniqueness.** It leaks another org's use of
  a repo and blocks two tenants tracking one repo (intent decision).
- **A partial unique index referencing `projects.archived_at`.** Neither
  SQLite nor Postgres allows a cross-table partial index. The nullable
  `active_claim` column gives the same guarantee.
- **An Alembic data migration for the backfill.** The repo fills identity
  at runtime (`_upsert`), and a migration could not read `settings` or git
  remotes safely.
- **Widening `GET /api/projects` for service keys.** It mixes user and
  service semantics and would carry local paths and settings.
- **Reusing `project:read` for the registry.** It maps to viewer on every
  project route through `scope_ceiling`.
- **Extending `normalize_repo_identity`.** It would change the
  `repoIdentity` existing readers see.

## Risks

- **A nullable `api_keys.user_id`** touches code that assumes an owner:
  `ApiKey.user`, `_lookup_by_digest` owner checks, and the WebSocket
  rejection (`auth.py:236`). Tests cover each path.
- **The backfill on large registries** runs once and is idempotent.
  Startup is held under the advisory lock on Postgres. The backfill logs
  progress and never blocks startup on a single bad row.
- **Path-prefixed GitLab** makes `namespace` carry the prefix. CFactory
  must strip it using its connection's web path. That is documented in the
  D6 consumer notes and in #574's spec.
- **Unresolved providers** stay usable but are flagged until an admin
  fixes them.

## Verification

- **Unit, `repo_ref`.** Every accepted form:
  - GitHub, `api.github.com`, GHE `/api/v3`;
  - GitLab with subgroups, port and path prefix;
  - Azure DevOps `_git`;
  - scp-like, bare.

  Plus rejections (local, file://, userinfo) and `match_key` case-folding.
- **Migration.** SQLite upgrade and downgrade per
  `test_project_identity_migration.py`, and Postgres under
  `tests/postgres/`:
  - existing rows survive;
  - `api_keys.user_id` becomes nullable;
  - constraints exist.
- **Store.**
  - Primary invariant.
  - Claim uniqueness within an org, and the same repo allowed in two orgs.
  - Archive releases the claim; reactivation gives a conflict when the
    repo is taken.
  - Backfill: oldest wins, idempotent, unresolved provider, remote-less
    project has zero repos.
  - `POST /api/projects` creates the primary, and a same-org duplicate
    gives 409.
- **Routes.**
  - Identical 404 for unknown and invisible projects; 403 for a
    low-role caller.
  - `repo_id` under another project gives 404.
  - Every D4 operation and its errors.
- **Service keys.**
  - Mint needs org admin and refuses key callers and non-service scopes.
  - The key is ownerless and works on the registry read.
  - The route-table sweep shows it is refused everywhere else.
  - Revoke works.
  - `last_used_at` is throttled.
- **Registry read.** The identical 404 for each listed caller kind, and
  multi-tenant filtering. The payload has no `path` or `settings`.
  `include_archived` works.
- **Audit.** One event per D7 action, with the listed fields.
- **Frontend.** vitest covers both sections, plus typecheck and lint.
- **CI suites.** `pytest tests/ -m "not slow"`,
  `pytest apps/web-server/tests`, Postgres and the cq-ratchet all pass.
- **Live (after release).**
  - Mint a `registry:read` key in the org settings UI and read
    `/api/orgs/{org}/registry`. Both demo projects should be listed with
    their primary repos.
  - The legacy shared token gets 404 on that route.
  - An existing user key still works everywhere else.
