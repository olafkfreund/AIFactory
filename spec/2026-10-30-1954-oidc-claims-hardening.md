---
status: draft
issue: 1954
intent: intent/2026-10-30-1954-oidc-claims-hardening.md
---

# Spec: make the #324 OIDC ID-token hardening actually apply

## Design

In `server/oidc/client.py`, add `ID_TOKEN_SIGNING_ALGS` (RS/ES/PS 256/384/512) and
`async def authorize_access_token(oidc: Any, request: Request) -> dict[str, Any]`:

1. `metadata = await oidc.load_server_metadata()`, then set
   `metadata["id_token_signing_alg_values_supported"] = ID_TOKEN_SIGNING_ALGS`.
   Authlib caches this dict and reads it again when it decodes the token.
2. Return `await oidc.authorize_access_token(request, claims_options=...)`, where
   `iss` is essential with values `[issuer, issuer + "/"]` (the issuer is
   `APP_OIDC_ISSUER_URL.rstrip("/")`; Azure AD v1 and Auth0 issue `iss`
   with a trailing slash), `aud` is essential with values `[client_id]`, and
   `exp` is essential.

Remove the dead `claims_options` and `id_token_signing_alg_values_supported`
from `register()`. Keep PKCE. `oidc_callback` calls the helper. Remove the
two kwargs-inspection tests in `tests/test_oidc_claims_options.py` and keep
the PKCE test.

## Alternatives rejected

- Keep the register-time kwargs: they do nothing and suggest protection that
  isn't there.
- Subclass the authlib app: more code for the same effect.

## Risks

- An IdP whose `iss` differs from the configured issuer by more than a
  trailing slash now fails login.
- The fix depends on authlib's metadata dict being cached and mutable. The
  behavioural test catches it if authlib changes that.

## Verification

`tests/test_oidc_id_token_hardening.py` runs real signed JWTs through authlib
1.9.1 with only the network stubbed. Cases:

- Valid RS256 accepted, with the configured issuer both with and without a
  trailing slash.
- Rejected: wrong `iss` (even when discovery agrees), missing `exp`, wrong
  `aud`, `aud`=other with `azp`=client_id, HS256 and `alg=none` (even when
  discovery advertises them).
- Mutation control: calling authlib directly, without the helper, accepts the
  HS256 token. This proves the test detects a missing pin.
