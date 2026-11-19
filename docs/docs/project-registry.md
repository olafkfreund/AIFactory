---
title: Project registry
---

# Project registry

AIFactory keeps its projects in the database (`projects` table). `projects.json` is no longer read or written by the server; it is only a one-off import source and a rollback export. (#2157, epic Factory#3857 phase 1)

## Identity

| Field | Rule |
| --- | --- |
| `id` | The existing uuid. Never changes. |
| `slug` | `a-z`, `0-9` and `-`, 1 to 64 characters, unique per organization. Derived from the name when not given (`my-app`, then `my-app-2` on a clash). Rename with `PUT /api/projects/{id}` `{"slug": "new"}`; the id stays. Archived slugs stay reserved. |
| `repoIdentity` | `host/namespace/repo`, lower case, from the clone URL or the checkout's `origin`. `null` for folders without a remote. Recorded only: projects are never merged by it. |
| `teamId` | The organization's default team. Teams and memberships are stored; access still follows organization membership. |

The path and settings of a project are not part of its identity and stay as they are.

## API

- `GET /api/projects`, `GET/PUT /api/projects/{id}` return `slug`, `teamId`, `repoIdentity` in addition to the old fields.
- `GET /api/projects/by-slug/{slug}[?org_id=...]` resolves a slug among the projects you can see (409 when several organizations hold it and no `org_id` is given).
- `POST /api/projects` accepts an optional `slug`.
- `DELETE /api/projects/{id}` archives the project: it disappears from lists and lookups, files and history stay. It answers 409 while a task of the project is running.

## Import and rollback

On first start the server imports an existing `projects.json` (new ids only, ids and all other fields preserved) and renames it to `projects.json.imported`. Running it again changes nothing.

Rollback to a release that still reads `projects.json`, with the service stopped:

```bash
python -m server.project_store export     # writes projects.json from the database
# deploy the previous release
```

Rolling forward again after the old release changed the file:

```bash
python -m server.project_store import --overwrite   # replaces rows from the file, archives rows missing from it
```

The new columns and tables are additive; the old release ignores them.
