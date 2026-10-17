---
status: approved
issue: 1730
spec: spec/2026-10-17-1730-optin-api-backstage.md
---

# Plan: publish the opt-in API surfaces to Backstage

Self-contained. You do not need to open the intent or the spec.

**Problem.** `scripts/generate-openapi-spec.py` pins the SAML, SCIM,
mcp-remote and rmux env flags off (`:33-48`), so the Backstage entity
`aifactory-web-api` (`catalog-info.yaml:84-111`, `$text:
./apps/web-server/openapi.yaml`) omits the 13 routes they add (5 SAML under
`/api/auth/saml/`, 4 SCIM under `/scim/v2/`, 2 under `/api/mcp-remote/`, 2
`/api/tasks/{spec_id}/agent-console/{attach,detach}`). Nothing changes in
existing paths when the flags are on.

**Approved decisions.**

1. D1 `--opt-in` argv flag on the existing generator (no new script). It
   pins the same five env vars to `"true"`, keeps only paths absent from the
   committed `apps/web-server/openapi.yaml`, exits non-zero if none remain,
   sets `info.title` = `AIFactory Web API (opt-in surfaces)`, a one-line
   `info.description` naming the flags, `info.version` = `opt-in` (constant:
   `scripts/bump-version.js:271-296` only bumps `openapi.yaml`, so a tracked
   version would drift every release), and writes
   `apps/web-server/openapi-optin.yaml`. No-arg behaviour is unchanged.
2. D2 One new API entity `aifactory-web-api-optin`, same shape as
   `aifactory-web-api`, added to `aifactory.providesApis` and the header
   comment. `aifactory-mcp` keeps its Transport B text.
3. D3 CI (`techdocs.yml`) generates and drift-checks the opt-in file; the CI
   drift check is the regression test (no new pytest: there is no logic in
   app code, and the generator imports the whole app).
4. D4 `components.schemas` is kept whole (known limit, documented in the
   generator with a `shortcut:` comment).
5. D5 The stale inline regeneration comment at `catalog-info.yaml:105-109`
   is replaced by the script command.

## Steps

Env for local runs: `PY=/mnt/code/Source-home/GitHub/AIFactory/apps/web-server/.venv/bin/python`.

### Step 1 — generator `--opt-in` (codex)

File: `scripts/generate-openapi-spec.py`.

- Docstring: add the `--opt-in` usage line and one sentence on what it writes.
- `OPT_IN = "--opt-in" in sys.argv[1:]`; `OUT` becomes `openapi-optin.yaml`
  when set, else `openapi.yaml` (keep the `BASE = WEB_SERVER / "openapi.yaml"`
  path available for reading).
- `dict.fromkeys(..., "true" if OPT_IN else "false")`. Update the comment
  above it to say the opt-in run pins them on.
- After `spec = app.openapi()`, when `OPT_IN`: load base paths with
  `yaml.safe_load(BASE.read_text(encoding="utf-8"))["paths"]`, filter
  `spec["paths"]` to keys not in it, `return 1` with a message on stderr if
  empty, update `info` per D1. Add the D4 `shortcut:` comment.
- The print line stays (it reports the output path and path count).

Check:
```
cd <worktree> && cp apps/web-server/openapi.yaml /tmp/base.yaml
APP_DISABLE_AUTH=true $PY scripts/generate-openapi-spec.py --opt-in   # 13 paths
APP_DISABLE_AUTH=true $PY scripts/generate-openapi-spec.py            # unchanged
git diff --exit-code apps/web-server/openapi.yaml
ruff check scripts/generate-openapi-spec.py && ruff format --check scripts/generate-openapi-spec.py
```
Traps: the opt-in run must not write `openapi.yaml`; do not reorder the env
pin after the `server.main` import; if `static/` exists locally the base run
gains `GET /` (D13 of #1632) — run with no `apps/web-server/static/`.

### Step 2 — commit the generated file, catalog entity, docs (lead)

- Add `apps/web-server/openapi-optin.yaml` from step 1's run.
- `catalog-info.yaml`: header entity-model comment gets
  `API aifactory-web-api-optin (openapi 3.1, opt-in SAML/SCIM/mcp-remote/rmux)`
  and the `aifactory` line gains `web-api-optin`; `providesApis` gains
  `aifactory-web-api-optin`; new `kind: API` document after
  `aifactory-web-api`; D5.
- `docs/api.md`: a short "Opt-in surfaces" section after "Interactive Swagger".

Check: `$PY -c "import yaml; d=list(yaml.safe_load_all(open('catalog-info.yaml'))); print([x['metadata']['name'] for x in d])"`
and `test -f apps/web-server/openapi-optin.yaml`.

### Step 3 — TechDocs workflow (codex)

File: `.github/workflows/techdocs.yml`.

- Both `paths:` lists (`:30`, `:44`): add `"apps/web-server/openapi-optin.yaml"`
  after `openapi.yaml`.
- Step 2 run block (`:169`): add a second line, the same command with
  `--opt-in`, after the base run; extend the comment above (`:160-163`) by
  one sentence (#1730, it reads the committed base spec so runs second).
- Step 4 (`:182`, `:189`, `:200`): add `apps/web-server/openapi-optin.yaml`
  to the three `git diff` path lists and the `--opt-in` command to the echoed
  fix instructions.
- Step 5 is unchanged (it only checks `openapi.yaml`).

Check: `$PY -c "import yaml; yaml.safe_load(open('.github/workflows/techdocs.yml'))"`,
`grep -c openapi-optin .github/workflows/techdocs.yml` (expect 6).
Trap: do not change step 5 or any other job.

## Tests

- The CI TechDocs job reruns both generator modes and fails on drift of
  either file. Before this change the opt-in file does not exist, so the
  catalog `$text` would be broken; after it, the job is green.
- Local checks per step above.

## Rollback

Revert the PR: removes the entity, the file, the generator flag and the CI
lines. No runtime state.
