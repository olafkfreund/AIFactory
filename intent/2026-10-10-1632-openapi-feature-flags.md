---
status: draft
issue: 1632
author: olafkfreund
---

# Intent: the OpenAPI spec must not depend on the generator's environment

## Problem

The issue is still open, and dev (1c2df772) does not fix it.

`scripts/generate-openapi-spec.py` builds `apps/web-server/openapi.yaml` from
whatever environment it runs in. It does not set or clear any feature flags.
Two loaders bring local flags in: `server/env_bootstrap.py` (loads
`apps/web-server/.env` with `setdefault`) and the pydantic `Settings` in
`server/config.py` (`env_file=".env"`, prefix `APP_`, resolved against the
current working directory). Flags exported in the shell also leak in.

Four routers are only mounted when their flag is on, and each changes the
spec:

| Flag | Extra paths |
|---|---|
| `APP_RMUX_ENABLED` / `AIFACTORY_RMUX_ENABLED` | 2 (`agent-console/attach`, `detach`; the WS route is not in OpenAPI) |
| `AIFACTORY_MCP_REMOTE_ENABLED` | 2 (`/api/mcp-remote/...`) |
| `SAML_ENABLED` | 5 (`/api/auth/saml/...`) |
| `SCIM_ENABLED` | 4 (`/scim/v2/...`) |

The always-mounted `/api/tasks/{task_id}/agent-console/sse` is why the
committed spec already shows some agent-console content.

`apps/web-server/.env.example` sets `APP_RMUX_ENABLED=true`. A developer who
copied it and runs the generator as its docstring says gets 297 paths. CI
(`techdocs.yml`, `refresh-and-validate`) runs with no `.env` and gets 295, the
committed spec. The drift gate then fails, and the fix hint it prints does not
mention the flags. Nothing records which flag set the published spec is built
with.

## Proposed outcome

- Running the generator gives byte-identical `openapi.yaml` locally (with any
  `.env`, from any working directory) and in CI.
- The flag set the spec is built with is fixed and documented in the generator
  and the CI comment.
- The committed `openapi.yaml` is regenerated with that set, and
  `refresh-and-validate` is green.

## Affected users and systems

- Developers and the release bump that regenerate the spec locally.
- CI: `.github/workflows/techdocs.yml`, job `refresh-and-validate`.
- Backstage API-tab readers of the published spec.
- Files: `scripts/generate-openapi-spec.py`, `apps/web-server/openapi.yaml`,
  `.github/workflows/techdocs.yml` (comment and hint only).
- Not affected: the running server and Helm deployments.

## Constraints

- Change only the generator. The runtime gating in `server/main.py`,
  `rmux/integration.py` and `mcp_remote/__init__.py` stays as it is. The
  SAML/SCIM gating is deliberate (test contamination), and mounting these
  routers by default would add attack surface to every deployment.
- The output must not depend on either loader or on exported shell flags.
- All four flag families are covered, not just rmux.
- `continue-on-error` stays off (#906).
- Any flag turned on must import cleanly in CI's fresh venv (SAML needs
  `python3-saml` and xmlsec), with no DB or network access at import.
- No changes to `.env.example`, Helm values or pod env defaults.
- Do not edit `scripts/bump-version.js` (that is #1631). Whichever of #1631 and
  #1632 merges second regenerates `openapi.yaml` on top of the first. The same
  applies to #1671 if it adds routes.

## Open questions

1. Which flag set is the canonical spec: all off (the default deployment,
   smallest published surface, matches today's committed spec) or all on (the
   full API in Backstage)?
2. If all on: is it fine to publish the SAML, SCIM and remote-MCP endpoint
   shapes in Backstage, and to have CI depend on xmlsec and `python3-saml`
   importing cleanly?
3. Should flag-gated routes be marked in the spec (tag, `x-` extension, or
   separate files), or is one unmarked spec enough?
4. Pinning a fixed list will not catch a fifth env-gated router added later.
   Should we add a guard, such as a test that generates the spec under two
   environments and checks the output is identical, or is pinning the four
   known families enough?
5. Ship #1631 and #1632 in one PR, or separately with a set merge order?
