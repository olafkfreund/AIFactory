---
status: approved
issue: olafkfreund/Factory#312
author: olafkfreund
---

# Intent: DISABLE_AUTH off loopback needs its own opt-in, not APP_DEBUG

## Problem

The epic olafkfreund/Factory#312 (IAM and access control) asks to gate `DISABLE_AUTH`.
CFactory's intent `2026-10-26-312-api-fail-closed` lists this as remaining slice 2.

`enforce_disable_auth_safety` (`apps/web-server/server/config.py`) refuses to boot
with `APP_DISABLE_AUTH=true` on a non-loopback `APP_HOST` unless `APP_DEBUG=true`.
`APP_DEBUG` is set for other reasons: uvicorn reload, verbose errors, and the
shipped `apps/web-server/.env.example` has `APP_DEBUG=true` uncommented. So turning
on debug silently also allows an unauthenticated admin API on `0.0.0.0`, which is the
image default host.

PFactory and TFactory already use a dedicated `APP_ALLOW_INSECURE_AUTH` opt-in and do
not consult debug (checked on origin/dev 2026-10-26: PFactory
`Settings._validate_disable_auth_host`, TFactory `Settings.assert_safe_auth_binding`).
So only AIFactory needs this change.

## Proposed outcome

With `APP_DISABLE_AUTH=true` on a non-loopback host, AIFactory refuses to boot unless
`APP_ALLOW_INSECURE_AUTH=true`. `APP_DEBUG` no longer relaxes this guard. Loopback
binds and auth-enabled boots are unchanged.

## Affected users and systems

- AIFactory web server boot guard (`config.py`), its tests, `.env.example` and the
  environment reference doc.
- Live cluster: none. Checked read-only: nothing in factory-gitops `apps/*/manifests`
  sets `APP_DEBUG` or `APP_DISABLE_AUTH`.
- Local dev: the nix dev shell and `docker-compose.yml` do not set `APP_DISABLE_AUTH`
  (compose sets `APP_DEBUG=false`), so nothing there needs the new flag. CI and pytest
  never reach the guard (it runs only from `main.__main__`).
- A developer who today runs `APP_DEBUG=true APP_DISABLE_AUTH=true` on `0.0.0.0` must
  add `APP_ALLOW_INSECURE_AUTH=true` or bind `127.0.0.1`; the refusal message says so.

## Constraints

- Must not lock out any login, change gitops/cluster config, rotate credentials or deploy.
- Must not touch `mcp_stdio` files (AIFactory #1885 in flight).
- Same env name as PFactory/TFactory: `APP_ALLOW_INSECURE_AUTH`.

## Open questions

None.

## Remaining slices (in order, after this one)

1. Wildcard-CORS boot guard (`enforce_cors_safety`) also reuses `APP_DEBUG`; consider
   the same treatment (not part of #312's wording; noted only).
2. Retire `CFACTORY_MCP_SECRET` in favour of scoped `CFACTORY_API_KEYS` entries.
3. Retire the shared `APP_API_TOKEN` per edge in favour of `acw_` service keys (Factory#555).
4. Enforce MFA/TOTP (+ WebAuthn) as a Keycloak required action, in gitops.
5. Fleet RBAC role model and quarterly access-review evidence.
