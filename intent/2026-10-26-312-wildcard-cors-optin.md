---
status: draft
issue: olafkfreund/Factory#312
author: olafkfreund
---

# Intent: the wildcard-CORS boot guard needs its own opt-in, not APP_DEBUG

## Problem

`enforce_cors_safety` (`apps/web-server/server/config.py`) refuses to boot when
`APP_CORS_ORIGINS` contains `*`, unless `APP_DEBUG=true`. `APP_DEBUG` is set for
unrelated reasons (reload, verbose logs, `/docs`; the shipped `.env.example` sets it),
so debug mode silently also accepts a wildcard origin. This is the same defect the
sibling slice `2026-10-26-312-insecure-auth-optin` (PR #1895) fixes for `DISABLE_AUTH`.

PFactory and TFactory have no wildcard-CORS boot guard and no opt-in for it (they strip
`*` or pass the list through), so there is no existing name to reuse.

## Proposed outcome

A wildcard origin refuses to boot unless `APP_ALLOW_WILDCARD_CORS=true`. With the
opt-in, the server warns and runs with credentialed CORS dropped, exactly as debug does
today. `APP_DEBUG` no longer affects this guard. An explicit allowlist is unchanged.

## Affected users and systems

- AIFactory boot guard, its tests, `.env.example`, environment reference doc.
- Live cluster: none. factory-gitops `apps/*/manifests` (origin/main) sets no CORS
  variable and no `APP_DEBUG` (checked read-only 2026-10-26).
- nix dev shell, `docker-compose.yml`, Helm chart, CI: none set `APP_CORS_ORIGINS`.
  The guard runs only from `main.__main__`, never under TestClient.

## Constraints

- No gitops/cluster/credential change, no deploy, no login lock-out.
- `cors_allows_credentials` and the middleware wiring stay as they are.
- Keep PR #1895 unchanged; this is a separate PR off origin/dev.

## Open questions

None.
