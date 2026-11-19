---
status: draft
issue: 2157
intent: intent/2026-11-19-2157-aif-project-identity.md
---

# Spec: AIFactory project identity registry on the DB

## Design

**One store, same call shape.** About 40 modules use the dict API `load_projects()` / `save_projects()` from `server/project_registry.py`. Keep that API and swap what is behind it, so every caller (authz at `routes/project_authz.py:149`, create at `routes/projects.py:692-718`, WebSocket authz, MCP, services) moves to the DB in one change. A new module `server/project_store.py` is the DB layer; `project_registry.py` delegates to it and keeps its other helpers.

**Sync API over an async DB.** The engine is async-only (aiosqlite / asyncpg) and callers are sync, some inside a running loop. The store runs a dedicated daemon thread with its own event loop and its own small pooled async engine (same URL, same SQLite WAL listener, `pool_kwargs`), and sync calls are `run_coroutine_threadsafe(...).result(timeout=10)`. Always the same loop, so asyncpg connections are never shared across loops. Hot async paths (`authorize_project_for_user`, `list_projects`, `add_project`) use awaitable twins `aload_projects()` / `asave_projects()` that `await asyncio.wrap_future(...)`, so they never block the server loop; other sync callers block for one small indexed read, like the file read they replace. The one-off import runs at startup from `seed_tenant_defaults` (before serving), with the lazy first-use import only as fallback.

**Row mapping, lossless.** Real columns: `id`, `org_id`, `name`, `path`, `slug`, `repo_identity`, `team_id`, `archived_at`. Every other key of the legacy entry (`created_at`, `updated_at`, `tenant_id`, `clonedFrom`, `clonedBranch`, `settings` incl. tokens and auto-fix queue, unknown keys) is stored as JSON in the existing, unused `projects.settings_json`. `load_projects()` rebuilds the same dict (blob keys + columns), so callers see no difference except new keys `slug`, `team_id`, `repo_identity`. Archived rows are excluded from `load_projects()`, exactly like a deleted entry today.

**Write semantics.** `save_projects(d)` is upsert-only and never removes. `load_projects()` returns a `dict` subclass that remembers the serialized snapshot of each entry it loaded; `save_projects` writes only entries that differ from that snapshot (a plain dict without one is compared with the DB). So handler A saving a stale copy of project B, which it did not change, does not overwrite B's concurrent update; only entries a caller edited are written. An interleaved-write test covers it. Two handlers editing the same project still last-write-wins, as with the file today. Removal is the explicit `archive_project(id)` (sets `archived_at`, row and id stay). This avoids the lost-update archive-by-omission a replace-style save would cause under concurrent handlers. The one `del projects[id]` caller (`routes/projects.py:798`) becomes `archive_project`. An upsert ensures the owning org (and the default service user it needs as owner) exists, mirroring `seed_tenant_defaults`, so a project with an org not yet seeded does not fail the FK.

**Identity.**
- `id`: unchanged uuid, never edited.
- `slug`: `^[a-z0-9]+(-[a-z0-9]+)*$`, 1-64 chars, unique per org (`UNIQUE(org_id, slug)`), archived slugs stay reserved. Derived from `name` when absent (lowercase, runs of other characters become `-`, trimmed, truncated to 56 so a collision suffix of up to 7 characters still fits in 64, empty becomes `project`), with `-2`, `-3` suffixes on collision. Rename = `PUT /projects/{id}` with `slug`; the id does not change; clash is 409, invalid is 400.
- `repo_identity`: `host/namespace/repo`, lowercase, from `clonedFrom` or the checkout's `origin` remote, via a pure `normalize_repo_identity(url)` handling https, ssh://, scp-like `git@host:ns/repo.git`, credentials stripped, `.git` and trailing slash stripped, nested namespaces kept. NULL when there is no remote. Recorded only; never used to match or merge (non-unique column).
- `Team(id, org_id, name, slug, is_default, created_at)` with `UNIQUE(org_id, slug)`; `TeamMembership(id, team_id, user_id, role, provenance, created_at)` with `UNIQUE(team_id, user_id)`; `role` validated against `ROLE_LEVELS`, `provenance` in `sso|manual`. `Project.team_id` nullable FK. An upsert without a team assigns the org's default team, created on first use. Phase 1 stores teams only: visibility and authz still follow org membership exactly as today (enforcement is phase 2), so local and single-user mode are unchanged.

**One-off import and rollback.** On first store use per process, if `projects.json` exists in `PROJECTS_DATA_DIR`, every entry whose id is not in the DB (archived included) is inserted with its id, slug derived, repo identity derived (git call with a short timeout, failure gives NULL); then the file is renamed to `projects.json.imported`. Re-running is a no-op; a rename failure only logs (rows are never hard-deleted, so nothing is resurrected). Rollback is a recovery procedure for a stopped service: `python -m server.project_store export` writes active projects back to `projects.json`, after which the previous release works; the DB rows stay. Rolling forward again after the old release changed the file needs `python -m server.project_store import --overwrite`, which replaces existing rows from the file and archives rows absent from it; the default import skips existing ids and so would keep stale data.

**Direct file readers move over.** `routes/worktree_merge.py` (nine sites: `:98,583,1020,1377,1712,1866,2086,2282,2567`), `routes/mcp.py:36`, `routes/terminal.py:387`, `routes/pr.py:131` read the JSON file themselves; they switch to `load_projects()`. After this no code reads or writes `projects.json` except the importer and the export command.

**Registry API (existing routes, extended).** `GET /projects` (org-filtered as today) and `GET/PUT /projects/{id}` gain `slug`, `teamId`, `repoIdentity`. New `GET /projects/by-slug/{slug}` (optional `?org_id=`) resolves the slug within the caller-visible orgs (409 when several visible orgs hold it and no `org_id` is given, 404 when none), then applies `require_project_access("viewer")` on the resolved id (registered before `/{project_id}`). `POST /projects` accepts optional `slug`. `DELETE /projects/{id}` archives (admin, unchanged), returns 409 while any task of the project is running on either backend (`get_agent_service().get_running_task_ids()`, ids `project_id:spec_id`). A task admitted in the same instant is not excluded (best effort); once archived, the project drops out of `load_projects()`, so no new task can resolve it. Responses never include tokens. Archived rows stay in the DB and are readable through the store (`get_project(id, include_archived=True)`) for audits and restore; no HTTP route exposes them in this phase.

## Alternatives rejected

- Separate "factory-identity" service: more moving parts, not needed to fix the dropdown (epic).
- Making all 40 callers async DB calls: huge diff, high regression risk; the dict facade gives the same single store.
- Dual-write JSON and DB: two sources of truth, the problem being fixed.
- Replace-style `save_projects` that archives missing ids: concurrent handlers would archive each other's new projects.
- Merging projects by repo identity or name: Codex finding, wrong for forks and monorepos; identity is recorded only.
- Deleting rows on DELETE: loses audit and slug reservation; archive keeps both.

## Risks

- Per-call DB read replaces a file read: one small SELECT on a pooled connection, 10 s timeout; sync callers block on it like they did on the file read, hot async paths do not.
- Another team adds an alembic revision in parallel: rebase and re-point `down_revision` before the PR.
- Two stores during a rolling deploy: old pods still read the JSON for a moment; documented, single replica today.
- Tests that wrote `projects.json` now seed the DB: fixed in the same change, via a conftest fixture that gives each test a temp SQLite registry.
- Tokens now live in `projects.settings_json` (plaintext, same as the JSON file today). Not worsened; encrypting is out of scope.

## Verification

- Unit: slug derive/validate/collision/truncation, repo normalization table, row-to-dict round trip including unknown keys, import idempotence with id preservation, export round trip, upsert-only semantics, archive hides from `load_projects`, stale-snapshot save does not overwrite a concurrent update, no event-loop stall while a store call is delayed, `--overwrite` roll-forward, default team assignment, running-task 409 (subprocess and kubejob ids), rename 409/400, by-slug 404/403.
- Tests fail without the change (they exercise DB-only behaviour: no `projects.json` is written by create).
- Migration test: upgrade creates the new columns and tables; downgrade restores.
- Whole `apps/web-server/tests` suite, ruff and the repo ratchet scripts.
- `codex review --base origin/dev` and a read against `plan/`.
