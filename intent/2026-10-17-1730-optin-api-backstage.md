---
status: draft
issue: 1730
author: olafkfreund
---

# Intent: publish the opt-in API surfaces to Backstage

## Problem

Since #1632, `scripts/generate-openapi-spec.py` pins `SAML_ENABLED`,
`SCIM_ENABLED`, `AIFACTORY_MCP_REMOTE_ENABLED`, `AIFACTORY_RMUX_ENABLED` and
`APP_RMUX_ENABLED` to false (`:33-48`), so `apps/web-server/openapi.yaml`, the
`$text` of the `aifactory-web-api` entity (`catalog-info.yaml:84-111`), is the
default deployment's surface only. Checked on `dev`: with every flag on the
app has 13 more paths (307 vs 294) and no changed ones:

- SAML: `/api/auth/saml/{login,acs,metadata,logout,sls}` (tag `Auth (SAML)`)
- SCIM: `/scim/v2/Users[/{user_id}]`, `/scim/v2/Groups[/{group_id}]` (`SCIM`)
- mcp-remote: `/api/mcp-remote/sse`, `/api/mcp-remote/messages/` (`MCP-Remote`)
- rmux: `/api/tasks/{spec_id}/agent-console/{attach,detach}` (`rmux Live Console`)

An operator who turns one of these on finds no published contract for it in
Backstage. Follow-up D14(b) of `plan/2026-10-10-1632-openapi-feature-flags.md`.

## Proposed outcome

- A second Backstage `kind: API` entity, `aifactory-web-api-optin`, provided
  by `aifactory`, whose OpenAPI definition is exactly the routes the flags add.
- The definition is generated from the app, like `openapi.yaml`, and the
  TechDocs workflow fails when the committed copy drifts.
- `aifactory-web-api` and `openapi.yaml` are unchanged.

## Affected users and systems

- `catalog-info.yaml`, `scripts/generate-openapi-spec.py`,
  `.github/workflows/techdocs.yml`, `docs/api.md`, one new generated file.
- No runtime, server, Helm or UI change.

## Constraints

- Docs/catalog only; the generator runs offline in CI, never in a deployment.
- Must not break the release bump (`scripts/bump-version.js:271-296` rewrites
  `info.version` in `openapi.yaml` only).
- Small; follow the existing `aifactory-web-api` pattern.

## Open questions

1. One combined opt-in entity or one per surface? Taken: one, since the four
   surfaces are the same server and the same generator run.
