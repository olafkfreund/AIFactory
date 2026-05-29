# Design — SAML 2.0 Single Logout (SLO) for AIFactory v1.2 (#209)

> Locked decisions: 6 (all below). Implementation PR: `feat/v1.2-saml-slo`.
> Base: v1.1 decision #11 deferral in `2026-05-28-saml-scim-design.md`.

---

## 1. Why — closing the v1.1 deferral

v1.1 decision #11 deliberately punted SLO:

> "User clicks 'Sign out' in AIFactory → clear our session cookie →
> redirect to the login page. We do NOT issue SAML LogoutRequest to
> the IdP."

That was correct at the time — shipping local-only logout let v1.1 land on
schedule while the SAML core (assertion, ACS, replay defence, metadata) was
being built and audited. The explicit cost was documented: operators who
wanted IdP-side session kill had to use the IdP's admin console or short TTLs.

**Compliance asks that motivate v1.2 SLO:**

- **ISO 27001 A.9.4.2** — "Secure log-on procedures" is satisfied by PKCE +
  MFA. But auditors in the financial-sector trials also ask about *log-off*
  propagation: "If an administrator revokes a user in the IdP, does AIFactory
  end their session within one authentication round-trip?" With v1.1 local-only
  logout, the answer is: only via token expiry (15-min access token + operator-
  configured refresh TTL). That gap is acceptable for low-risk contexts but
  flags in FedRAMP-adjacent and EU-regulated deployments.
- **ADFS + Azure AD operators** have explicitly requested SLO in the field-
  trial feedback. Both IdPs support SAML SLO (HTTP-POST binding) well; the
  feature is operationally proven.
- The gap is asymmetric: v1.1 already handles IdP-initiated *login* (IdP-init
  ACS flow). Not handling IdP-initiated *logout* is inconsistent from the IdP
  admin's perspective.

---

## 2. Locked decisions

### D-1: SP-initiated logout flow

```
User → POST /api/auth/saml/logout
  → Read NameID from access_token cookie (email claim)
  → Build signed LogoutRequest (OneLogin SDK auth.logout())
  → Mint HMAC RelayState binding {nameid, nonce, exp}
  → 302 to IdP SLO URL with SAMLRequest + RelayState
  → IdP propagates to other SPs (cross-SP propagation is the IdP's job)
  → IdP 302s browser back to POST /api/auth/saml/sls with SAMLResponse + RelayState
  → SP validates RelayState HMAC + SAMLResponse
  → Kill SP session (clear access_token + refresh_token cookies)
  → 302 to login page
```

The SP uses `SAML_IDP_SLO_URL` (derived from IdP metadata or overridden via
Helm `saml.slo.idpSloUrl`). The OneLogin SDK's `auth.logout()` constructs
the LogoutRequest XML, signs it with the SP key, and returns the redirect URL.

### D-2: IdP-initiated logout flow

```
IdP → POST /api/auth/saml/sls with SAMLRequest (LogoutRequest XML)
  → SP validates: signature against IdP cert, audience = sp_entity_id,
                  NotOnOrAfter > now
  → SP checks replay cache for LogoutRequest ID (D-4)
  → SP extracts NameID from validated request
  → SP clears access_token + refresh_token cookies in the response
  → SP returns signed LogoutResponse body to IdP:
    - Status: Success  (normal case)
    - Status: NoSession (NameID not matched to a live session — see note)
```

For IdP-init, the SP responds with an HTTP 200 that contains either a SAML
LogoutResponse auto-submit form (when the IdP's SLO URL is known from
metadata) or a plain 200 with the response body that the IdP can process.
The OneLogin SDK's `process_slo()` builds the LogoutResponse XML.

Why NoSession instead of 4xx for unmatched NameIDs: returning 4xx on a
logout for a NameID the SP does not recognise causes some IdPs (including
certain ADFS versions) to treat the propagation as a failure and abort the
chain, leaving other SPs logged in. NoSession is the spec-correct
signal that the SP has no session for this NameID; the IdP treats it as
success and continues propagation.

### D-3: Binding — HTTP-POST only

SLO supports HTTP-POST binding only. HTTP-Redirect is excluded because:

1. Redirect bindings embed the LogoutRequest/Response in the URL query
   string. For large responses with long signatures, URL length limits on
   some proxies and browsers (2048–8192 characters) can truncate them.
2. HTTP-POST is universally supported by Azure AD, ADFS, Okta, Keycloak,
   and PingFederate. No operator trial has requested Redirect-only SLO.
3. Reducing the attack surface: a POST body is not logged in most proxy /
   WAF access logs the way query parameters are.

The SP metadata advertises only HTTP-POST `<SingleLogoutService>` binding.
IdPs that support only HTTP-Redirect SLO will not be able to use this
feature; they fall back to local-only logout (same behaviour as v1.1).

### D-4: Replay defence extension

The existing `SamlReplayCache` (from v1.1) is extended to cover
`LogoutRequest` IDs in addition to assertion IDs. The same per-ID-TTL
semantics apply: the TTL for each LogoutRequest entry is its own
`NotOnOrAfter` minus now. We do NOT use a separate cache instance — one
`SamlReplayCache` per process covers both, keyed by the XML `ID` attribute.
LogoutRequests and assertions share the same ID namespace in SAML; there
is no semantic collision risk.

### D-5: Session-kill semantics — current session only

When a logout event (SP-init confirmation or IdP-init request) is
validated, the SP:

1. Deletes the `access_token` and `refresh_token` HTTP-only cookies by
   setting them to expired (max-age=0, same path and domain as set on login).
2. Does NOT revoke all sessions for the same user across other tabs or
   other `OidcRefreshSession` rows.

**Why current-session-only?** The SAML SLO spec is single-session: the
IdP propagates one LogoutRequest per SP per session. Revoking all sessions
DB-wide would violate the user's expectation that logging out from one
browser tab does not close their other open sessions on other devices.
Operators who need full-user revocation (security incident response) use
the IdP's admin revoke-all or the AIFactory admin API (out of scope here).

### D-6: SP metadata update — advertise SLS endpoint only when enabled

When `saml.slo.enabled=true`, the SP metadata endpoint
(`GET /api/auth/saml/metadata`) adds a
`<SingleLogoutService Binding="...HTTP-POST..." Location=".../sls"/>` element
to the `EntityDescriptor`. When `saml.slo.enabled=false` (default), the
element is absent. This ensures that existing IdP metadata uploads (which
pre-date v1.2) are not broken when operators upgrade: the IdP metadata
already uploaded does not reference the SLS endpoint, so the IdP will not
attempt SLO initiation. The operator opts in by:

1. Setting `saml.slo.enabled=true` in Helm values.
2. Re-uploading the SP metadata XML at the IdP (the metadata endpoint
   now advertises the SLS binding).
3. Configuring the IdP to use the SLS URL for logout propagation.

The `saml.slo.idpSloUrl` is either:
- Derived automatically from the IdP metadata
  (`<SingleLogoutService Binding="...HTTP-POST..." Location="...">` element); or
- Overridden explicitly in `values.yaml` (for IdPs whose metadata does not
  advertise SLO or whose SLO URL differs from the metadata).

Default: derive from metadata. Explicit override wins.

---

## 3. Out of scope

- **Front-channel SLO** (`<iframe>`-based logout propagation to multiple SPs
  simultaneously from the IdP's logout page). Parking lot for v1.3+.
- **HTTP-Redirect SLO binding** (see D-3 rationale).
- **Partial logout** (logout from one SP, keeping others active from the same
  IdP session). Trust the IdP to propagate; AIFactory kills only its own session.
- **Multi-SP propagation from AIFactory's side** — cross-SP propagation is
  the IdP's job. AIFactory is always an SP, never an IdP aggregator.
- **SAML Artifact binding** — not used in v1.1 and not added in v1.2.

---

## 4. Threat model

### Replay attack (D-4 closes this)

An attacker who captures a signed LogoutRequest (e.g. via a compromised
proxy before TLS termination) and replays it would terminate a live session.
The replay cache rejects any LogoutRequest ID seen before, within the
request's own `NotOnOrAfter` window. Same defence as assertion replay in v1.1.

### RelayState tampering (D-1 + existing relay_state.py closes this)

An attacker who modifies the RelayState during the SP-init round-trip could
redirect the post-logout location to an attacker-controlled URL. The HMAC-
signed RelayState from v1.1 is reused: the SLS handler verifies the HMAC
before trusting any embedded `return_to`. Tampered RelayState → 400.

### Unauthenticated SLO requests (D-2 closes this)

An attacker who POSTs a crafted LogoutRequest to `/sls` without a valid IdP
signature would forge a session-kill. The OneLogin SDK's `process_slo()`
validates the signature against the IdP certificate (from the loaded
metadata). Unsigned or invalid-signature LogoutRequests → 400.

### NameID mismatch

A LogoutRequest whose NameID does not match the user who owns the SP session
cookie would silently kill the wrong user's session. Mitigation: for SP-init
confirmation, the RelayState embeds the NameID of the user who initiated the
logout; the SLS handler verifies they match. For IdP-init, the SP compares
the LogoutRequest NameID against the `email` claim in the access-token cookie
when present; when the cookie is absent or expired it returns NoSession (D-2)
rather than killing any session.

**Residual risk**: SAML NameID is an IdP-asserted value; the SP trusts the
IdP signature. A compromised IdP can issue a LogoutRequest for any NameID.
This is identical to the trust boundary for authentication; it is outside
the SP's control.

### Cookie theft after logout

After the SP clears cookies (D-5), a stolen `access_token` JWT remains
cryptographically valid until its 15-minute expiry. This is unchanged from
v1.1 — SLO does not add a revocation mechanism for access tokens. Operators
requiring sub-15-min revocation should use the short-TTL access-token
configuration option.

---

## 5. SDK surface used

```python
# SP-initiated logout (build LogoutRequest + redirect URL):
auth = OneLogin_Saml2_Auth(request_data, old_settings=client.build_settings_dict())
slo_redirect_url = auth.logout(
    return_to=relay_state_token,  # HMAC-signed RelayState (relay_state.mint)
    name_id=email_from_cookie,    # user's email = SAML NameID
    session_index=None,           # not tracked in v1.2
)

# SLS endpoint — process SAMLRequest (IdP-init) or SAMLResponse (SP-init confirm):
url, error_msg, stay_on_page = auth.process_slo(
    keep_local_session=False,
    request_id=None,
    retrieve_parameters_from_server=False,
)
# url: where to redirect post-logout (for IdP-init inline response)
# error_msg: non-empty on failure
# stay_on_page: True = IdP-init, inline LogoutResponse returned
```

---

## 6. Decision audit summary

| # | Question | Decision |
|---|----------|----------|
| D-1 | SP-init flow shape | Full SAML SLO round-trip via LogoutRequest/Response |
| D-2 | IdP-init handling | Accept LogoutRequest, return NoSession on miss (no 4xx) |
| D-3 | Binding | HTTP-POST only |
| D-4 | Replay defence | Extend existing `SamlReplayCache` to LogoutRequest IDs |
| D-5 | Session-kill scope | Current session only (per-tab logout, no DB-wide revoke) |
| D-6 | SP metadata | Advertise SLS endpoint only when `saml.slo.enabled=true` |
