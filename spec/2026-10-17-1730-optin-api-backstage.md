---
status: approved
issue: 1730
intent: intent/2026-10-17-1730-optin-api-backstage.md
---

# Spec: publish the opt-in API surfaces to Backstage

## Design

1. **Generator mode `--opt-in`** in `scripts/generate-openapi-spec.py`. Same
   flag list, pinned to `"true"` instead of `"false"`. After `app.openapi()`:
   - keep only the paths not in the committed `apps/web-server/openapi.yaml`
     (read with `yaml.safe_load`); fail (exit non-zero) if none remain, which
     means a flag stopped taking effect;
   - set `info.title` to `AIFactory Web API (opt-in surfaces)`, a description
     naming the four flags, and `info.version` to the constant `opt-in`;
   - write `apps/web-server/openapi-optin.yaml`.
   Without `--opt-in` the script is unchanged.
2. **Catalog**: new `kind: API` `aifactory-web-api-optin` in
   `catalog-info.yaml`, a copy of `aifactory-web-api`'s shape (`type: openapi`,
   `definition.$text: ./apps/web-server/openapi-optin.yaml`), its description
   naming each surface and its enabling env var. Add it to `aifactory`'s
   `providesApis` and the header's entity-model comment. Also replace the
   stale inline regeneration comment at `:105-109` with the script command.
3. **CI** `.github/workflows/techdocs.yml`: step 2 also runs the generator
   with `--opt-in` (after the base run, since it reads `openapi.yaml`); step 4
   checks and prints `openapi-optin.yaml` too; both `paths:` lists gain it.
4. **Docs** `docs/api.md`: one short section naming the new entity and the
   regeneration command.

## Alternatives rejected

- **The full all-flags-on spec as the second entity**: duplicates the 294
  default paths in Backstage; readers cannot see which routes are opt-in.
- **Hand-written text definition** (like `aifactory-mcp`): rots; no CI check.
- **An entity per surface**: four generator runs or a tag split for 13 paths.
- **Tracking the release version**: `bump-version.js` only rewrites
  `openapi.yaml`, so a copied version would make the drift check fail on
  every release. A constant avoids touching the release script.
- **Filtering by tag**: a new route with a new tag would be silently dropped;
  diffing against the committed base spec is exact.

## Risks

- The opt-in file is derived from the committed `openapi.yaml`; if that is
  stale the opt-in diff is wrong. CI step 4 fails on the base drift in the
  same run, so the fix is to regenerate both.
- `components.schemas` is kept whole (unused schemas shown in Swagger). Known
  limit; pruning needs a `$ref` walk nobody asked for.
- A future env gate must be added to the flag list (already documented there).

## Verification

- Local: run the generator both ways; `openapi.yaml` is unchanged and the
  opt-in file has the 13 paths listed in the intent.
- `yaml.safe_load` of `catalog-info.yaml` succeeds; the new entity's `$text`
  path exists.
- `ruff check` / `ruff format --check` on the script; CI TechDocs job green
  (it reruns the generator and diffs both files, which is the regression test).
