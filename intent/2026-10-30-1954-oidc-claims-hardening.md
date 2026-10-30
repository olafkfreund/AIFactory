---
status: draft
issue: 1954
author: olafkfreund
---

# Intent: make the #324 OIDC ID-token hardening actually apply

Refs olafkfreund/Factory#312.

## Problem

`apps/web-server/server/oidc/client.py` passes `claims_options` and an
asymmetric-only `id_token_signing_alg_values_supported` to `oauth.register()`
(#324 M2). Authlib 1.9.1 (the requirements floor) never reads them when it
validates the ID token. `register()` stores unknown kwargs as server metadata,
and `client_kwargs` go to the HTTP session. `parse_id_token` takes claim
rules only from `authorize_access_token(claims_options=)` and the algs only
from the loaded discovery metadata. So `iss` is checked only against what
discovery says, and HS256 is accepted if discovery advertises it.
`tests/test_oidc_claims_options.py` only inspects the register kwargs, so it
passes anyway.

## Proposed outcome

The callback rejects an ID token with `iss` other than the configured issuer
(with or without a trailing slash), `aud` without our client_id, no `exp`,
or an alg outside RS/ES/PS 256-512 (including `none`), whatever discovery
says. A test proves these settings are what authlib actually uses.

## Affected users and systems

AIFactory web-server OIDC login (`/api/auth/oidc/callback`).

## Constraints

- Must not lock out OIDC logins; existing OIDC tests pass.
- Same pattern as PFactory #953 / TFactory #1460.

## Open questions

None.
