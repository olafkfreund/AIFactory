# SAML test fixtures (Epic #35 #41)

This directory contains **intentionally low-security** RSA-2048 self-signed
certificates used solely by the SAML test suite. They never reach
production code paths.

| File | Purpose | Where it's used |
|------|---------|-----------------|
| `sp-test.crt` / `sp-test.key` | Test SP signing cert | `tests/saml/conftest.py` configures the OneLogin SDK with this pair so the SP can build / sign AuthnRequests and (when encrypted-assertion tests run) decrypt incoming assertions. |
| `idp-test.crt` / `idp-test.key` | Test IdP signing cert | The IdP-fixture helpers use this key to sign assertions that the SP-under-test then validates with the corresponding cert. Mimics what a real IdP would do. |

## Security note for the secret scanner

These keys are committed deliberately. They are:

1. Generated with a 100-year expiry — anyone who finds the repo
   already has them.
2. Bound to test-only CNs (`test-sp.example.com`, `test-idp.example.com`).
3. Loaded only by the test suite, never by the production app
   (`apps/web-server/server/saml/client.py` reads its cert from the
   path supplied by the `SAML_SP_CERT_FILE` env var; the test fixture
   points that env at this directory).

If automated secret scanning flags these as findings, mark them as
**accepted risk — test fixtures**. Hard-coded test material in
public OSS repos is a long-standing industry pattern (cf. requests,
flask, oauthlib).

## Regenerating

If the certs ever need rotation (e.g. an algorithm upgrade), regenerate
with:

```bash
openssl req -x509 -newkey rsa:2048 \
  -keyout sp-test.key -out sp-test.crt \
  -days 36500 -nodes -subj "/CN=test-sp.example.com"

openssl req -x509 -newkey rsa:2048 \
  -keyout idp-test.key -out idp-test.crt \
  -days 36500 -nodes -subj "/CN=test-idp.example.com"
```

Then re-run the SAML test suite to confirm the IdP cert's public-key
fingerprint still matches whatever the SDK records.
