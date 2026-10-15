---
status: draft
issue: 1729
author: olafkfreund
---

# Intent: CI fails when feature flags change the generated OpenAPI spec

## Problem

`scripts/generate-openapi-spec.py:38-50` pins five env gates off (#1632) so
`apps/web-server/openapi.yaml` is the default deployment's API whatever the
caller's `.env` or shell says. Nothing in CI proves that pin works: the
techdocs drift check (`.github/workflows/techdocs.yml:163-200`) runs only in a
clean environment, where every flag is already off. If the pin regresses (a
flag dropped from the list, a gate that starts reading a cached setting, a
second name for an existing gate), a developer with a flag on commits a spec
with opt-in routes and CI cannot tell why.

## Proposed outcome

- CI regenerates the spec a second time with every known flag exported as
  `true`, and fails if the result differs from the committed spec.
- The failure message says the generator pin, not staleness, is the problem.

## Affected users and systems

`.github/workflows/techdocs.yml` (TechDocs workflow), `scripts/generate-openapi-spec.py`
comments. No runtime code.

## Constraints

- No new dependency; reuse the venv the workflow already builds (`/tmp/ws`).
- A naive "app with flags on equals app with flags off" check is red by design:
  SAML, SCIM, mcp-remote and rmux add routes when on. The comparison must go
  through the generator, whose output is supposed to be flag-independent.

## Open questions

- Limit, recorded instead of asked (bulk run, conservative choice): no on/off
  check can toggle a gate whose name it does not know. A brand-new gate that
  defaults off is already caught by the existing drift check, which runs in a
  clean env; one that defaults on is part of the default deployment by design.
  So this task guards the pin, and does not try to discover unknown gates.
