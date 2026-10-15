---
status: approved
issue: 1731
intent: intent/2026-10-15-1731-openapi-root-route.md
---

# Spec: the OpenAPI spec no longer depends on static/ being absent

## Design

- **D1, hide the placeholder from the schema.** `apps/web-server/server/main.py:735`
  becomes `@app.get("/", include_in_schema=False)`. The route still serves the
  same JSON when `static/` is absent; with `static/` the SPA mount is unchanged.
  Both cases now produce a spec without `/`.
- **D2, regenerate `apps/web-server/openapi.yaml`.** The only change is removing
  the `/:` block (`:10022-10031`); 295 paths become 294.
- **D3, drop the "static/ must be absent" wording** from the generator docstring
  (`scripts/generate-openapi-spec.py:11-12`) and from the drift-check message
  (`.github/workflows/techdocs.yml:197`).
- **D4, one regression test** `apps/web-server/tests/test_openapi_root_route.py`:
  `create_app().openapi()["paths"]` has no `"/"` (the worktree has no `static/`,
  so this is the placeholder branch, which is the one that leaked). Same
  `pytest.importorskip("server.main")` pattern as `tests/test_stale_tasks.py:215-217`.

## Alternatives rejected

- A constant `GET /` route registered always: with `static/` it would shadow the
  SPA's `index.html` at `/`, a runtime change.
- Deleting the placeholder: loses the dev hint and changes `GET /` from 200 to 404 in dev.
- Stripping `/` in the generator: fixes only the generator, not `/openapi.json` at runtime.

## Risks

- A client generated from the spec that called `root__get`: none known; it is a
  dev-only placeholder. Low.

## Verification

- New test fails on `dev`, passes after D1.
- Regenerate: prints `294 paths`; `git diff --stat apps/web-server/openapi.yaml`
  shows only the 10 removed lines.
- TechDocs workflow green on the PR (drift check).
