# Design — SAML 2.0 + SCIM 2.0 for legacy-IdP banks (Epic #35 #41)

> **STATUS: shipped 2026-05-28** — closed by #41. Implementation PRs: #177, #178, #195, #196, #198, #199. See [CHANGELOG.md](../../CHANGELOG.md) for the v1.1 release notes.

> Locked from super-brainstorm 2026-05-28. Reviewer-style audit pass
> next; implementation in 2 PRs after sign-off.

## Why we're doing this

OIDC (v1.0) covers Okta / Auth0 / Google / GitHub fine. The orgs we
*can't* sell into today are the ones whose identity team mandates SAML
2.0 (ADFS-era, regulated banks, some EU public-sector). They also
expect SCIM 2.0 so HR offboard → IdP suspend → AIFactory account
disabled happens automatically without a human in the loop.

v1.1 ships both, behind opt-in toggles, alongside OIDC. Existing
deployments are unaffected.

## Out of scope (explicit)

- **Other SAML profiles** (ECP, single-attribute query, etc.) — Web SSO
  only.
- **Just-in-time provisioning via SAML** alone — we expect SCIM for
  the org-chart-changes-overnight case. Email-match on first SAML
  login auto-links to an existing User row (created by either OIDC or
  SCIM); we do NOT auto-create users from a SAML assertion if no
  matching row exists. Rejecting is safer than creating ghost users.
- **Full SAML Single Logout (SLO)** — local logout only. See decision
  #11 below.
- **OAuth flows beyond Bearer** for SCIM — no client-credentials
  grant in v1.1.
- **Multi-tenant per-IdP routing** — one Helm release = one IdP set
  (operator can configure N OIDC + 1 SAML IdPs, but cross-tenant
  isolation lands in #36).
- **OpenShift / FedRAMP cert hardening** — values.yaml accepts any
  PEM; we don't enforce FIPS-mode crypto.

## Locked decisions

### 1. SAML library — `python3-saml` (OneLogin)

Smaller surface, single XML schema, batteries-included signature /
encryption helpers, used by every enterprise SaaS we've audited.
Heavier C deps: needs `xmlsec1`, `libxml2-dev`, `libxmlsec1-dev`,
`libxmlsec1-openssl` apk packages in the web-server Dockerfile.

**Alternative considered:** `pysaml2` (IdentityPython). More flexible
but verbose; non-Web-SSO profiles we don't need anyway.

**Risk acceptance:** The xmlsec1 dep historically had CVEs (CVE-2022-...).
We accept this because (a) it's the canonical SAML stack in the Python
ecosystem and (b) we control update cadence via the base image bump.

### 2. Login UX — Single page + IdP discovery dropdown

Operator configures a list of named IdPs in `values.yaml`. Each IdP is
typed: `oidc | saml`. The login page renders them as a dropdown +
"Sign in" button. Picking an OIDC IdP redirects to its authorization
endpoint; picking a SAML IdP redirects to its SSO URL with our
AuthnRequest.

```yaml
identityProviders:
  - name: "Corp SSO (SAML)"
    kind: saml
    metadataUrl: https://idp.example.com/saml/metadata
  - name: "Eng GitHub"
    kind: oidc
    ref: oidc.presets.github
```

The existing `oidc:` top-level block stays for backward compat — its
configured IdP appears in the dropdown as "OIDC (default)" when
`identityProviders` is empty.

### 3. SCIM auth — Static Bearer token from K8s Secret

```yaml
scim:
  enabled: false
  tokenSecretName: ""    # required when enabled, key: SCIM_BEARER_TOKEN
```

The SCIM endpoint reads `SCIM_BEARER_TOKEN` env on startup; every
request to `/scim/v2/*` must carry `Authorization: Bearer <token>`. A
constant-time compare avoids timing oracle. Rotation = recreate Secret
+ restart pod (the SDK reads at startup, not per-request).

**Why not per-IdP tokens / OAuth?** Either would force a settings UI +
DB table + lifecycle endpoint. v1.1 scope says "make Okta and Azure AD
work"; both expect a single Bearer.

### 4. User collision — Match-and-link by verified email (with cross-IdP guard)

When a SAML assertion arrives with `nameid=alice@corp.com`:
1. Look up `users` by `email='alice@corp.com'` (case-insensitive).
2. If not found → reject with 403 "user not provisioned"; SCIM is
   expected to have created the row first.
3. If found, check `external_ids`:
   - If `external_ids[saml]` is already set to this `nameid` → activate session (idempotent re-login).
   - If `external_ids[saml]` is unset AND **no** `external_ids` of
     other kinds (`oidc:*`) exist → attach `external_ids[saml] =
     nameid`, activate session.
   - If `external_ids` of a DIFFERENT IdP kind already exist (e.g.
     `external_ids[oidc:github]`) → **reject with 409 cross-IdP linkage
     requires admin confirmation**. Linking is performed via the
     authenticated-session admin flow (out of v1.1 scope; v1.2 adds
     the UI). Operator runbook: manually update `external_ids` in
     DB after verifying the human identity.

**Why the cross-IdP guard?** The reviewer pointed out a real attack:
an attacker who controls a GitHub account that doesn't verify
corporate email could link via OIDC, then a corp SAML IdP asserts
the same email and silently links into the same User row. Rejecting
auto-link across IdP kinds closes this. Same-kind re-link (e.g.
two SAML IdPs both asserting the same email) is also rejected for
the same reason.

**Requires the IdP to verify email.** Operator's runbook says: enable
"Verify email on signup" in the IdP. We don't have a way to enforce it
at the SP — but the trust boundary is the IdP signature anyway, so
the email is as trustworthy as the IdP.

**Why not auto-create?** Two failure modes the audit team called out:
(a) typo'd emails create ghost users, (b) misconfigured IdP federation
hijacks accounts. Both close cleanly by requiring SCIM-provisioned
rows first.

### 5. SCIM scope — Full CRUD on Users + Groups, soft-delete

| Method | Path | Behaviour |
|--------|------|-----------|
| POST | /scim/v2/Users | Create. 201 + resource. 409 on email collision. |
| GET | /scim/v2/Users | List with filter + paging per RFC 7644. Soft-deleted users (`active=false`) excluded by default; included only when filter has `active eq false`. |
| GET | /scim/v2/Users/{id} | Read. 404 if missing OR if soft-deleted (matches Azure AD's expectation that a deprovisioned user is gone). |
| PATCH | /scim/v2/Users/{id} | Per-attribute update per RFC 7644 §3.5.2. Supports `op: add | replace | remove` (case-insensitive parsing — Azure AD sends capitalised `Add`). |
| PUT | /scim/v2/Users/{id} | Whole-resource replace. |
| DELETE | /scim/v2/Users/{id} | **Soft delete** — sets `active=false`, preserves row + audit history. Returns 204. |
| (same shape for /Groups) | | Members list = User IDs. |

**Soft-delete + 404-on-GET pattern**: returning soft-deleted users
from GET would break Azure AD's sync (it re-GETs after DELETE and
expects 404). We resolve this by returning 404 on GET when
`active=false`, while preserving the row internally for audit
queries. The row is reachable only via admin tooling, not via SCIM.

**Multi-valued attribute PATCH semantics (RFC 7644 §3.5.2.1)**:
`op: add` on a multi-valued attribute (e.g. Group `members`) MUST
**append** to the existing array, not replace it. Implementing
add-as-replace is the most common SCIM bug; Azure AD relies on
append semantics for Group member sync. The `filters.py` PATCH
applier has dedicated handling for arrays.

**Concurrent-update header** (`If-Match`): accepted as advisory only.
We never return 412 Precondition Failed — Okta's SCIM client retries
with full PUT on 412, which can clobber unrelated fields. Last-writer-
wins, documented in the concept doc.

**SCIM filter grammar subset (v1.1)**: explicitly minimum-viable to
keep the parser small. Supported: `userName eq "..."`, `externalId
eq "..."`, `active eq true|false`. Operators: `eq` only. No bracket
grouping, no `and`/`or`. This covers 100% of what Okta + Azure AD
send during user-sync operations. Other operators return 400 +
`scimType: invalidFilter`. Documented in the concept doc.

### 6. NameID format — emailAddress

`urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress`. The
AuthnRequest's `NameIDPolicy` advertises this; assertions with other
formats are rejected at the SP. Matches our user-mapping rule above.

**Explicit identity-key constraint across protocols:**
```
scim.userName == saml.NameID == users.email
```
SCIM's `userName` and SAML's `NameID` MUST both be the user's email
for v1.1. The provisioning layer enforces this on POST /scim/v2/Users
(if `userName != emails[0].value`, reject with 400). Operators who
need decoupled `userName` (e.g. `alice.smith` for `userName`,
`alice@corp.com` for `email`) wait for v1.2. Documented as a hard
constraint in the concept doc.

### 7. CI strategy — In-process

Same model as the OIDC tests (`tests/oidc/`). Fixtures:
- `make_signed_saml_response(subject_email, recipient_url, audience)`
  builds + signs an assertion using a test SP cert that we ship in
  `tests/fixtures/saml/`.
- `scim_client(api_token)` returns a `httpx.Client` pre-configured
  with the Bearer header against `TestClient(app)`.

Marker: `@pytest.mark.saml`. New CI job: **saml (P8 acceptance)**,
mirroring P3 (oidc).

**Reviewer recommendation pre-emptively applied:** the test fixture
SP cert is committed to the repo (it's intentionally low-security,
used only for test assertion signing). Include a README note in
`tests/fixtures/saml/` explaining that.

### 8. Helm shape — Separate `saml:` + `scim:` blocks

```yaml
saml:
  enabled: false
  spEntityId: ""              # required when enabled, e.g. "https://aifactory.example.com/saml"
  acsUrl: ""                  # required, e.g. "https://aifactory.example.com/saml/acs"
  idpMetadataUrl: ""          # one of: metadataUrl, metadataSecretName
  idpMetadataSecretName: ""   # Secret with key: idp-metadata.xml
  spCertSecretName: ""        # Secret with keys: cert.pem + key.pem (for signing AuthnRequests)
  requireEncryptedAssertion: false  # see decision #10 below

scim:
  enabled: false
  tokenSecretName: ""         # required when enabled, key: SCIM_BEARER_TOKEN
```

Validators (same pattern as `otel.headersSecretName` from #42):
- `saml.enabled=true` requires either `idpMetadataUrl` or
  `idpMetadataSecretName` (not both).
- `saml.enabled=true` requires `spEntityId` and `acsUrl`.
- `saml.requireEncryptedAssertion=true` without `saml.enabled=true`
  → fail (operator typo trap).
- `scim.enabled=true` requires `scim.tokenSecretName`.

### 9. SAML flow — Both SP-init + IdP-init

Three routes:
- `GET /saml/login?idp=<name>` — issues an AuthnRequest, redirects to
  IdP. (SP-init entry point.)
- `POST /saml/acs` — Assertion Consumer Service. Handles BOTH SP-init
  and IdP-init.
- `GET /saml/metadata` — public SP metadata XML for the IdP to consume.

**RelayState binding (SP-init CSRF defence).** The `GET /saml/login`
handler generates an HMAC-signed token:

```
relay = base64url(json({
  "nonce": <16 bytes random>,
  "exp": <now + 10 minutes>,
  "return_to": <validated absolute URL on our origin>,
  "idp": <idp name>,
}))
hmac = HMAC-SHA256(SECRET_KEY, relay)
relay_state = relay + "." + base64url(hmac)
```

This is sent as the AuthnRequest's `RelayState` parameter. On
`POST /saml/acs`, we verify HMAC + `exp` + `idp` against the assertion's
issuer. If `RelayState` is missing or invalid, we treat the flow as
IdP-init and use `saml.idpInitDefaultReturnTo` (absolute URL,
required when IdP-init is allowed; defaults to `/`).

`SECRET_KEY` is the existing FastAPI session secret (already required;
no new operator config).

**IdP-init lands on `saml.idpInitDefaultReturnTo` — an absolute URL.**
We don't accept a path-only value because behind a reverse proxy with
a path prefix, "relative to app root" is ambiguous. Validator: the URL's
scheme + host must match `saml.spEntityId`'s host.

### 10. Signature + encryption requirements

We require **signed Assertions specifically**, not merely signed
Responses. The OneLogin SDK settings dict hard-codes:

```python
"security": {
    "wantAssertionsSigned": True,    # the actual Assertion XML element
    "wantMessagesSigned": False,     # Response envelope signing optional
    "wantNameId": True,
    "requestedAuthnContext": False,  # leave to IdP defaults
    "signatureAlgorithm": "http://www.w3.org/2001/04/xmldsig-more#rsa-sha256",
    "digestAlgorithm": "http://www.w3.org/2001/04/xmlenc#sha256",
    "wantAssertionsEncrypted": <from values.yaml>,
}
```

`wantAssertionsSigned` matters because the IdP can sign just the
`<Response>` envelope, leaving the inner `<Assertion>` attributes
unprotected. We reject in that case.

**`strict=True` is hard-coded, never operator-configurable.** It enables
the SDK's defences against XML Signature Wrapping (XSW) attacks. If it
ever leaks into `values.yaml` we open CVE-2022-41912-class attacks.
Documented in code as a constant, not a config knob.

Encryption is optional — controlled by `saml.requireEncryptedAssertion`
(default `false`). When `true`, the SP needs its own cert in
`spCertSecretName`; OneLogin SDK decrypts using the SP key.

**SP cert rotation: `sp.x509certMulti` for overlap window.** The
OneLogin SDK accepts a list of SP certs (current + previous) so that
during the operator's rotation window, both certs are valid for
assertion-decryption / metadata-publishing. The SP cert Secret may
contain `cert.pem` (current) and optionally `cert.pem.previous` (the
prior one); both get loaded into `sp.x509certMulti`. Documented
runbook: rotate by populating `.previous`, restart pod, re-upload SP
metadata at the IdP, drop `.previous` after IdP-side propagation.

**Replay-attack defence**: in-process LRU of `(assertion_id, exp)`
pairs. The TTL on each entry is the assertion's own `NotOnOrAfter`
minus now — not a blanket 5-minute LRU. Pre-eviction sweep on
insertion drops entries whose TTL has elapsed. Bounded to 10k entries
per replica with LRU eviction on overflow (overflow under normal load
means an attack or misconfig and is logged at WARNING).

A blanket 5-minute LRU would mean an attacker who captures an Azure-AD
assertion (typical 60–120 min lifetime) could replay it successfully
after 5 minutes, for the remainder of the assertion's life. Per-
assertion-TTL closes that.

### 11. Logout — Local only

User clicks "Sign out" in AIFactory → clear our session cookie →
redirect to the login page. We do NOT issue SAML LogoutRequest to the
IdP. Operators wanting IdP-side session kill use the IdP's admin
console or short session TTLs.

Document this explicitly in the concept doc — some compliance reviewers
will ask.

### 12. SCIM rate-limit — Rely on ingress

No per-Bearer-token bucket in-process. Operators who want it set an
ingress annotation:
```yaml
nginx.ingress.kubernetes.io/limit-rps: "100"
nginx.ingress.kubernetes.io/limit-connections: "20"
```

## Implementation plan — split into 3 PRs (revised mid-implementation)

The original 2-PR plan assumed the User model already had a generic
`external_ids` JSONB column for the cross-IdP collision guard
(decision #4). Audit during PR-1 found it doesn't — the model has a
single `oidc_sub: String, unique, nullable` column. Adding a generic
external-IDs surface is a schema migration that deserves its own PR
to stay reviewable; routes that depend on it can't ship until the
schema lands.

Revised split:

- **PR-1a (this one)** — Security-critical foundation modules. No
  DB changes, no routes, no chart changes. Each module is reviewable
  in isolation.
- **PR-1b** — Schema migration (add `external_identities` table or
  `external_ids` JSONB) + SAML routes + SCIM CRUD routes +
  identity_providers + auth.py extension + tests.
- **PR-2** — Helm chart + concept doc + e2e (as originally planned).

### PR-1a — Security foundation (this PR)

- `apps/web-server/server/saml/replay_cache.py` — per-assertion-TTL
  replay defence + thread-safety.
- `apps/web-server/server/saml/client.py` — OneLogin SDK wrapper with
  XSW + signed-Assertion defences hard-coded.
- `apps/web-server/server/saml/relay_state.py` — HMAC-signed RelayState
  binding for SP-init CSRF defence.
- `apps/web-server/server/scim/schemas.py` — Pydantic models per
  RFC 7643 (User, Group, ListResponse, PatchOp, Error).
- `apps/web-server/server/scim/filters.py` — minimum-viable RFC 7644
  §3.4.2 filter parser (eq-only on userName/externalId/active).
- `apps/web-server/server/scim/auth.py` — Bearer-token middleware with
  constant-time compare + 503-on-misconfig.
- `tests/saml/` + `tests/scim/` — 48 unit tests across the above.
- `tests/fixtures/saml/` — committed test certs + README explaining
  they are intentionally low-security.

### PR-1b — Schema + routes + integration

- Alembic migration: add `external_identities` table OR
  `users.external_ids JSONB` (TBD; needs another decision before
  this PR starts).
- `apps/web-server/server/saml/routes.py` — /saml/login, /saml/acs,
  /saml/metadata with full cross-IdP collision guard.
- `apps/web-server/server/scim/routes.py` — full CRUD with array-
  append PATCH + soft-delete + 404-on-deleted.
- `apps/web-server/server/identity_providers.py` — merged view of
  OIDC + SAML IdP list for the login page.
- `apps/web-server/server/routes/login.py` — IdP discovery endpoint.
- `apps/web-server/server/auth.py` — extend `current_user_dependency`
  for SAML sessions.
- Integration tests against DB fixtures.

### PR-2 — Helm `saml:` + `scim:` blocks + concept doc + e2e

- `charts/aifactory/values.yaml`: `saml:` + `scim:` blocks.
- `charts/aifactory/values.schema.json`: validation.
- `charts/aifactory/templates/deployment.yaml`: env wiring +
  validators.
- `charts/aifactory/templates/configmap.yaml` (or equivalent): IdP
  metadata XML mount when `idpMetadataSecretName` is set.
- `tests/helm/test_saml_scim_toggle.py`: same shape as
  `test_otel_toggle.py`.
- `docs/docs/concepts/saml-scim.md`: when-to-use, IdP preset recipes
  (Okta / Azure AD / Keycloak), failure-safe contract.
- `docs/sidebars.ts`: add to concepts section.
- `.github/workflows/ci.yml`: add **saml (P8 acceptance)** job.

## Failure-safe contract

Same as OTel (#42): every SAML / SCIM code path wrapped in `try/except`.
A broken IdP doesn't crash the portal. A malformed SCIM request
returns a typed SCIM error (per RFC 7644 §3.12) instead of a 500.

## IdP metadata refresh — locked

Background task in `saml/client.py` refreshes `idpMetadataUrl` every
**4 hours** with exponential-backoff on failure (1 min → 2 min → 4 min
→ ... capped at 1 hour). The last-known-good XML stays cached
indefinitely; logins keep working through a transient IdP outage. If
the cache age exceeds **48 hours**, log a WARNING on every login attempt
(operators wire to their alerting). The task never crashes the pod —
all errors caught and logged.

When `idpMetadataSecretName` is used instead, no refresh — the operator
rotates by recreating the Secret + pod restart.

## Test matrix additions

Required fixture cases beyond the obvious happy paths:

- **Keycloak IdP-init with `InResponseTo` absent**: Keycloak doesn't
  emit `InResponseTo` on IdP-init flows; some strict SP validators
  reject. Test confirms we accept (treat as IdP-init).
- **Azure AD PATCH `Add` (capital) on Group `members`**: array-append
  semantics must work.
- **Azure AD DELETE then GET**: GET on soft-deleted user returns 404.
- **Replay attack**: same assertion submitted twice within its
  NotOnOrAfter window — second submission rejected.
- **XSW attack fixture**: assertion with a wrapped second `<Assertion>`
  block — rejected (validates `strict=True` is enforced).
- **Cross-IdP email collision**: SAML asserts email already linked to
  an OIDC user — rejected with 409.

## Open questions

(All previously-open items are now locked above. Remaining genuinely
open at the design level:)

- **Group membership semantics**: SCIM Groups have an opaque ID;
  should it map to our existing org/role tables or stay parallel?
  **Locked: parallel for v1.1**, integrate with #36 (tenant isolation)
  when that lands.
- **Login-page i18n**: the IdP dropdown labels are operator-supplied
  strings; do we localise them?
  **Locked: no** — operator chooses display language at config time.

## Decision audit summary

12 of 12 brainstorm decisions taken on recommended options. Reviewer
audit pass added 6 critical refinements (all baked in above) + 6
recommendations (all baked in above). No deviations from the brainstorm
intent — refinements tighten the design without changing scope.
