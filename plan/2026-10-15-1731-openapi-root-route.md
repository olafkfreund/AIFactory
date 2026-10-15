---
status: draft
issue: 1731
spec: spec/2026-10-15-1731-openapi-root-route.md
---

# Plan: the OpenAPI spec no longer depends on static/ being absent

Branch `fix/1731-openapi-root-route`, base `origin/dev` 7bcc9fe5. Line numbers
checked against that base. On the base the generator prints `295 paths` and
leaves `openapi.yaml` unchanged.

Env: `V=<main clone>/apps/web-server/.venv/bin` (has both requirement sets),
`B=<main clone>/apps/backend/.venv/bin` (ruff, pytest).

## Approved decisions (from the spec)

- **D1** `apps/web-server/server/main.py:735`: `@app.get("/")` →
  `@app.get("/", include_in_schema=False)`. Nothing else in the route changes.
- **D2** Regenerate `apps/web-server/openapi.yaml`; only the `/:` block
  (`:10022-10031`) goes; 294 paths.
- **D3** Remove the "static/ must be absent" wording from
  `scripts/generate-openapi-spec.py:11-12` and `.github/workflows/techdocs.yml:197`.
- **D4** Regression test `apps/web-server/tests/test_openapi_root_route.py`.
- Rejected: always-on `GET /` (shadows the SPA), deleting the placeholder,
  stripping `/` in the generator.

## Steps

1. `apps/web-server/tests/test_openapi_root_route.py` (new): one test,
   `main = pytest.importorskip("server.main")`, assert `"/" not in
   main.create_app().openapi()["paths"]`, docstring citing #1731.
   → verify: `cd apps/web-server && /pytest tests/test_openapi_root_route.py` FAILS on the base.
   Traps: follow `tests/test_stale_tasks.py:215-217`; the worktree has no `static/`.
2. `apps/web-server/server/main.py:735`: D1, plus a one-line comment
   `# include_in_schema=False: spec must not depend on static/ (#1731)`.
   → verify: step 1's test passes; `/ruff check` and `/ruff format --check` on the file.
   Traps: do not touch the `SPAStaticFiles` mount.
3. `scripts/generate-openapi-spec.py:11-12`: docstring becomes
   "Output is pinned to all feature flags off (independent of .env, exported
   flags and cwd) and does not depend on apps/web-server/static/ (#1731)."
   `.github/workflows/techdocs.yml:197`: echo becomes
   `"  (output ignores .env, exported feature flags and apps/web-server/static/)"`.
   → verify: `/ruff format --check scripts/generate-openapi-spec.py`;
   `python -c "import yaml;yaml.safe_load(open('.github/workflows/techdocs.yml'))"`.
4. Regenerate: `APP_DISABLE_AUTH=true /python scripts/generate-openapi-spec.py`.
   → verify: prints `294 paths`; `git diff --stat apps/web-server/openapi.yaml` is `10 deletions`.
   Traps: never hand-edit the yaml.

Steps 1-4 are each a few lines; done by the lane agent directly (below the
handoff threshold for a one-line route change).

## Tests

- `cd apps/web-server && /pytest tests/test_openapi_root_route.py tests/test_spa_staticfiles_ws_guard.py` → pass.
- TechDocs workflow green on the PR.

## Rollback

Revert the PR. The spec regains `GET /`; no runtime effect.
