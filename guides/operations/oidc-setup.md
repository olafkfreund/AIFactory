# OIDC SSO setup runbook

> Audience: platform / SRE teams enabling OIDC single sign-on for
> AIFactory (Epic #26 P3). Compliance frameworks this supports:
> SOC2 CC6.1 (logical access), NIST 800-63B (federated identity),
> internal SSO mandates from corporate IT.
>
> Goal: configure AIFactory to delegate user authentication to an
> external Identity Provider (Keycloak, Okta, or Azure AD) instead
> of (or alongside) the local password flow.

## What ships in v1.0

| Feature | Status |
| --- | --- |
| Authorization Code + PKCE flow | ✅ |
| ID token + state + nonce validation | ✅ |
| JIT user + OrganizationMember provisioning | ✅ |
| Claim-mapped roles (group → role) | ✅ |
| Internal JWT minting (interchangeable with local-login JWT) | ✅ |
| Short access TTL (15 min) + refresh path | ✅ |
| IdP-validated refresh with userinfo caching | ✅ |
| Logout → IdP `end_session_endpoint` redirect | ✅ |
| Back-channel logout | ⏭ v1.1 |
| SAML 2.0 + SCIM 2.0 | ⏭ v1.1 |

The local-password login flow (`/api/auth/login`) continues to work
alongside OIDC — operators who need a break-glass admin path keep it.

## Configuration matrix

All settings are env vars (mapped from `values.yaml: oidc.*` in the
Helm chart for k8s deployments).

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `APP_OIDC_ENABLED` | yes (to enable) | `false` | Master feature flag. When false, `/api/auth/oidc/*` returns 404. |
| `APP_OIDC_ISSUER_URL` | yes | — | The IdP's OIDC discovery root (without `/.well-known/openid-configuration`). |
| `APP_OIDC_CLIENT_ID` | yes | — | Relying-party client id registered in the IdP. |
| `APP_OIDC_CLIENT_SECRET` | yes | — | Relying-party client secret (confidential client). |
| `APP_OIDC_REDIRECT_URI` | no | derived from `request.url_for("oidc_callback")` | Override only if reverse-proxy hostname differs from the request URL. |
| `APP_OIDC_PROVIDER` | no | `keycloak` | One of `keycloak`, `okta`, `azure_ad`. Controls preset claim conventions + default scope. |
| `APP_OIDC_SCOPE` | no | preset-specific | Override the IdP default scope (e.g., add `offline_access`). |
| `APP_OIDC_GROUP_TO_ROLE` | no | `{}` | JSON map: IdP group name → internal role. First match wins. |
| `APP_OIDC_DEFAULT_ROLE` | no | `member` | Fallback role when no group matches. |
| `APP_OIDC_DEFAULT_ORG_SLUG` | no | `default` | Slug of the org JIT-provisioned users join. |
| `APP_OIDC_DEFAULT_ORG_NAME` | no | `Default Organization` | Used only when the org has to be auto-created. |
| `APP_OIDC_POST_LOGIN_REDIRECT` | no | `/` | Where to send users after successful login. |
| `APP_OIDC_POST_LOGOUT_REDIRECT` | no | `/` | Where to send users after IdP-side logout. |
| `APP_OIDC_USERINFO_CACHE_TTL_S` | no | `300` | In-process userinfo cache lifetime. Set `0` to disable. |

## Endpoints

| Path | Method | Purpose |
| --- | --- | --- |
| `/api/auth/oidc/login` | GET | Begins the flow. Returns 302 to IdP. |
| `/api/auth/oidc/callback` | GET | IdP redirects here. Validates state+nonce, exchanges code, JIT-provisions, mints JWT, 302 to `APP_OIDC_POST_LOGIN_REDIRECT`. |
| `/api/auth/oidc/refresh` | POST | Validates refresh JWT + IdP liveness, mints new access token. Returns 401 on IdP rejection. |
| `/api/auth/oidc/logout` | POST | Deletes the OidcRefreshSession row, invalidates userinfo cache, 302 to IdP's `end_session_endpoint`. |

## Keycloak

Tested in CI; this is the reference path.

```bash
# Provision a realm + confidential client + test user. The values
# below match tests/oidc/fixtures/keycloak-realm.json which is what
# the secrets-acceptance job boots.
#
# Realm:    aifactory
# Client:   aifactory-web (confidential, PKCE S256 required)
# Secret:   <provision from values.yaml secretRef>
# Users:    add manually via the Keycloak admin console
```

Required Keycloak settings on the client:
- Access Type: `confidential`
- Standard Flow Enabled: `on`
- Direct Access Grants: `off` (PKCE-only flow)
- PKCE Method: `S256`
- Valid Redirect URIs: `https://your-aifactory.example.com/api/auth/oidc/callback`

Env wiring:

```bash
APP_OIDC_ENABLED=true
APP_OIDC_PROVIDER=keycloak
APP_OIDC_ISSUER_URL=https://keycloak.internal/realms/aifactory
APP_OIDC_CLIENT_ID=aifactory-web
APP_OIDC_CLIENT_SECRET=<from secret store>
APP_OIDC_GROUP_TO_ROLE='{"aifactory-admin": "admin", "aifactory-member": "member"}'
```

Configure Keycloak to emit the `groups` claim by adding a Group
Membership mapper to the client's Mappers tab (token claim name:
`groups`, full group path: off).

## Okta

```bash
# 1. In the Okta admin console: Applications → Create App Integration
# 2. OIDC — OpenID Connect → Web Application
# 3. Settings:
#    - Grant types: Authorization Code + Refresh Token
#    - Sign-in redirect URI: https://your-aifactory.example.com/api/auth/oidc/callback
#    - PKCE: Required
# 4. After creation, note client_id + client_secret.
# 5. Sign On → Group Claim Filter: emit `groups` claim with the
#    matching groups (e.g. starts with "AIFactory/").
```

Env wiring:

```bash
APP_OIDC_ENABLED=true
APP_OIDC_PROVIDER=okta                                # adds 'groups' scope by default
APP_OIDC_ISSUER_URL=https://YOUR_TENANT.okta.com/oauth2/default
APP_OIDC_CLIENT_ID=<from Okta>
APP_OIDC_CLIENT_SECRET=<from Okta>
APP_OIDC_GROUP_TO_ROLE='{"AIFactory/Admin": "admin", "AIFactory/Member": "member"}'
```

## Azure AD (Entra ID)

```bash
# 1. Azure portal → App registrations → New registration
# 2. Supported account types: Single tenant (recommended) or multi-tenant
# 3. Redirect URI (Web): https://your-aifactory.example.com/api/auth/oidc/callback
# 4. After creation:
#    - Note Application (client) ID
#    - Certificates & secrets → New client secret → note the value
# 5. Token configuration → Add groups claim → emit Group ID
#    (object ID, NOT group name — Azure AD doesn't expose names by default)
# 6. API permissions → Microsoft Graph: openid, profile, email, User.Read
#    → Grant admin consent
```

Env wiring (note the OIDs in the role map — these are Azure AD group
object IDs, not display names):

```bash
APP_OIDC_ENABLED=true
APP_OIDC_PROVIDER=azure_ad
APP_OIDC_ISSUER_URL=https://login.microsoftonline.com/<tenant-id>/v2.0
APP_OIDC_CLIENT_ID=<application-client-id>
APP_OIDC_CLIENT_SECRET=<client-secret-value>
APP_OIDC_GROUP_TO_ROLE='{"00000000-0000-0000-0000-000000000abc": "admin", "00000000-0000-0000-0000-000000000def": "member"}'
```

For tenants with > 200 group memberships per user, Azure AD emits a
groups *overage* claim instead of inline group IDs — this requires
calling Microsoft Graph during the callback. **Overage is not
supported in v1.0**; the workaround is to use Azure AD's Application
Role assignments instead of groups (Token configuration → Add
optional claim → `roles`), and then set
`APP_OIDC_GROUP_TO_ROLE='{"Admin": "admin"}'` keyed on the role
name. P3.4-extension will fix this in v1.1.

## Verification

After wiring up an IdP:

1. **Discovery reachable**:
   ```bash
   curl -s "$APP_OIDC_ISSUER_URL/.well-known/openid-configuration" | jq .
   ```
   Must return a JSON document with `authorization_endpoint`,
   `token_endpoint`, `userinfo_endpoint`, `end_session_endpoint`,
   `jwks_uri`. Missing endpoints break specific flow steps.

2. **Login redirect emitted**:
   ```bash
   curl -sI "$AIFACTORY_URL/api/auth/oidc/login" | head -1
   # HTTP/1.1 302 Found
   ```

3. **End-to-end via browser**:
   - Click "Sign in with SSO" on the login page.
   - Authenticate with the IdP.
   - You should land on `/` (or `APP_OIDC_POST_LOGIN_REDIRECT`)
     with both `access_token` and `refresh_token` cookies set
     (DevTools → Application → Cookies).

4. **JIT provisioning row check**:
   ```sql
   SELECT id, email, oidc_sub, role FROM users
     WHERE oidc_sub IS NOT NULL ORDER BY created_at DESC LIMIT 5;
   ```
   New rows should have `oidc_sub` populated; existing rows that
   were upgraded from local-password to OIDC should keep their
   original `id`.

5. **Logout redirects to IdP**:
   - Click logout (or POST `/api/auth/oidc/logout`).
   - Network tab should show a 302 to `<issuer>/protocol/openid-connect/logout`
     (Keycloak) or the equivalent path for your IdP.
   - Cookies cleared (Max-Age=0).

## Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `404 OIDC SSO is not configured` from `/oidc/login` | `APP_OIDC_ENABLED` not `true` or required env vars missing | Set all four (ENABLED, ISSUER_URL, CLIENT_ID, CLIENT_SECRET). |
| `400 OIDC callback rejected` after IdP login | State or nonce mismatch; common with cross-origin redirects | Verify reverse-proxy is preserving cookies (especially `SameSite`). Check `APP_OIDC_REDIRECT_URI` matches the URI registered in the IdP exactly. |
| `400 ID token missing required claims (sub, email)` | IdP's profile/email scope not granted, or scope not requested | Ensure the IdP's app registration includes `openid profile email` in the consented scopes. |
| User logs in but lands in the wrong org | `APP_OIDC_DEFAULT_ORG_SLUG` points to an existing org or auto-creates a new one | Pre-create the target org with the right slug, OR set the slug to your existing org's slug. |
| All OIDC users get the default role despite group memberships | `APP_OIDC_GROUP_TO_ROLE` JSON malformed (parse error logged) or group claim not emitted | `jq .` the JSON value; check the IdP is actually emitting `groups` (see per-IdP setup above). |
| 401 on refresh after a few minutes | `APP_OIDC_USERINFO_CACHE_TTL_S=0` plus IdP unreachable | Set a positive cache TTL or fix the IdP connectivity. |

## Related

- [kms-rotation-runbook.md](kms-rotation-runbook.md) — Epic #26 P2 KMS rotation.
- Issue #30 — P3 acceptance criteria.
- Source: `apps/web-server/server/oidc/`, `apps/web-server/server/routes/oidc_routes.py`.
