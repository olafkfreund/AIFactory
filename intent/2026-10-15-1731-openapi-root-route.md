---
status: draft
issue: 1731
author: olafkfreund
---

# Intent: the OpenAPI spec no longer depends on static/ being absent

## Problem

`apps/web-server/server/main.py:728-741` mounts the built SPA at `/` when
`apps/web-server/static/` exists, and otherwise registers a placeholder
`@app.get("/")` (`:735`). The placeholder has no `include_in_schema=False`, so
it appears in the generated spec as `GET /` (`apps/web-server/openapi.yaml:10022`).
A checkout with a built frontend therefore generates one path fewer, and the
generator (`scripts/generate-openapi-spec.py:12`) and the techdocs drift check
(`.github/workflows/techdocs.yml:197`) have to tell people to delete `static/`.

## Proposed outcome

- The generated spec is the same with and without `static/`; `GET /` is not in it.
- The committed `openapi.yaml` is regenerated to match.
- The generator docstring and the drift-check message no longer require `static/` to be absent.
- `GET /` still returns the placeholder JSON in development.

## Affected users and systems

Backstage API docs (`apps/web-server/openapi.yaml`), the TechDocs workflow,
developers regenerating the spec. No runtime behaviour change.

## Constraints

- Do not change what `/` serves, with or without `static/`.
- Regenerate `openapi.yaml` in the same PR, or the techdocs drift check fails.

## Open questions

None.
