---
status: draft
issue: 1729
intent: intent/2026-10-15-1729-openapi-flag-guard.md
---

# Spec: CI fails when feature flags change the generated OpenAPI spec

## Design

- **D1, new last step in the `refresh-and-validate` job** of
  `.github/workflows/techdocs.yml`, after "Check generated docs are committed"
  (ends `:200`). Name: `Check the OpenAPI spec ignores feature flags`. It runs
  the generator again from `/tmp/ws` with `SAML_ENABLED`, `SCIM_ENABLED`,
  `AIFACTORY_MCP_REMOTE_ENABLED`, `AIFACTORY_RMUX_ENABLED` and `APP_RMUX_ENABLED`
  set to `true` (plus `APP_DISABLE_AUTH=true`), then
  `git diff --exit-code -- apps/web-server/openapi.yaml`. On a diff it prints an
  `::error::` naming the generator pin (`scripts/generate-openapi-spec.py`) and
  shows the diff. Because the previous step already proved the committed spec
  equals the flags-off output, "flags on == committed" means "flags on == flags off".
- **D2, comments.** Replace the generator's `ponytail` note (`:36-37`, "follow-up:
  two-env spec-equality guard") with a pointer to the new CI step, and say in it
  that a new gate's name must be added to both lists. Add one line to the
  techdocs comment block (`:160-162`) for the new step.
- **D3, no extra deps or config.** With the pin working, the SAML/SCIM routers
  are never imported, so the flags-on run needs nothing beyond `/tmp/ws`. If
  the pin breaks, the import of `python3-saml` (already in
  `apps/web-server/requirements.txt:120`) or the extra routes fail the step, which
  is the point.

## Alternatives rejected

- App on vs off with an allowlist of gated paths (bypassing the pin): only
  re-lists routes we already know are gated; it cannot see a gate it does not
  toggle, so it adds an allowlist to maintain and catches nothing the pin
  guard does not.
- A pytest that builds the app twice in one process: `get_settings()` caching
  and module-level imports make it order-dependent, and it would duplicate the
  generator's logic instead of testing the generator CI actually runs.
- Also writing the flags into `apps/web-server/.env`: process env already beats
  `env_file` (`server/config.py:246-250`), so the exported run covers the
  same pin.

## Risks

- Adds one generator run (a few seconds; the venv is reused) to the TechDocs job.
- If a flag is added to the pin but not to the CI step, it is not exercised;
  D2's comment names both places.

## Verification

- Locally, with the base generator: the D1 command leaves `openapi.yaml`
  unchanged (exit 0).
- Locally, with `AIFACTORY_RMUX_ENABLED` removed from the pin in a scratch copy:
  the D1 command exits 1 and the diff shows the rmux routes. Restore after.
- TechDocs workflow green on the PR, with the new step listed.
