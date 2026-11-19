---
status: draft
issue: 2157
author: olafkfreund
---

# Intent: AIFactory project identity registry on the DB

Epic: olafkfreund/Factory#3857 (phase 1). Owner decisions there: project lives in the URL, AIFactory owns the shared registry, the DB is the only store.

## Problem

Project authz reads `projects.json` (`routes/project_authz.py:149`, `project_registry.py`) and create writes it (`routes/projects.py:692-718`). The SQL `Project` model (`database/models.py:295`) has no readers, so there are two stores and one is dead. Projects have no stable human slug, no normalized repo identity (the same repo gets a different id per portal) and no team. Nothing can build `/p/:slug` URLs, a "my projects" list or team isolation until the registry is one authoritative store with these fields.

## Proposed outcome

- Every AIFactory read and write of the project list goes through the DB; `projects.json` is no longer read or written after a one-off, idempotent import that keeps existing ids. A documented rollback re-exports the DB to `projects.json`.
- A project has an immutable `id` (existing uuid), a unique renamable `slug`, and a normalized repo identity (host + full namespace; null for remote-less local folders).
- Minimal Team and TeamMembership tables (role from `ROLE_LEVELS`, provenance `sso|manual`), `Project.team_id`, and one default team per org, so single-user/local mode behaves as today.
- A registry API: list visible projects, get by id or slug, create, update/rename, archive, all behind `require_project_access`. Existing `/projects` routes and responses keep working and gain `slug`.

## Affected users and systems

AIFactory web-server (`project_registry.py`, `routes/projects.py`, `routes/project_authz.py`, about 40 `load_projects` callers, alembic). Direct `projects.json` readers outside `load_projects` are in scope too: `routes/worktree_merge.py:98,1020,1377,2086,2282` and `routes/mcp.py:36-46`, plus `get_projects_file()` users (`routes/terminal.py`). Existing frontends keep working unchanged. PFactory, TFactory, CFactory are untouched (phases 2/3); service-principal bypass is untouched.

## Constraints

- Identity is separate from the checkout path: per-project path and settings stay as they are.
- No name-based merging of projects; repo identity is only recorded, never used to merge here.
- Deletion is archive by default; files and history are untouched.
- Local/single-user mode and `DISABLE_AUTH` keep working with no team setup.
- Registry entries carry credentials (`gitToken`, `githubToken` mirrored by `routes/context.py:788-823`). Import, rollback export and DB storage keep them exactly as `projects.json` does today (same trust level); the new registry API responses never return them.
- Import and rollback are lossless for every existing field: `org_id`, `tenant_id`, `clonedFrom`, `clonedBranch`, `settings`, auto-fix queue, timestamps. Unknown keys are kept, not dropped.
- Archive is refused (409) while a task is running for the project; archived projects leave every list and lookup exactly as a deleted one does today, but stay in the DB and are readable by id for admins.
- No change to service-principal behaviour.
- Migration is additive (new columns/tables); the schema revert leaves old code working.

## Open questions

None. Delegated decisions (owner delegation 2026-11-19): archived slugs stay reserved (never reused); the importer renames nothing and derives slugs from the name with `-2`, `-3` collision suffixes.
