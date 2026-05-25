"""P3 — OIDC SSO acceptance tests.

Six tests map directly to the six acceptance bullets in Epic #26
issue #30:

  1. test_login_callback_pkce_roundtrip
     — Authorization Code + PKCE happy path against Keycloak. Verifies
       the full /login -> /callback -> session-cookie chain.

  2. test_pkce_state_tamper_rejected
     — Negative test. Tampering the `state` param on callback raises
       and produces no session.

  3. test_jit_provisions_user_and_org_member
     — First login from an unknown `sub` mints a User row + an
       OrganizationMember row with the claim-mapped role.

  4. test_userinfo_cache_avoids_per_request_rtt
     — N successive API calls under a single refresh window hit the
       cache; the IdP's userinfo endpoint is called once, not N times.

  5. test_user_disabled_in_idp_revoked_within_ttl
     — Disabling a user in the IdP causes the next refresh-token round
       (within 15 min) to fail.

  6. test_logout_redirects_to_end_session_endpoint
     — POST /api/auth/oidc/logout issues a redirect to the IdP's
       end_session_endpoint (when advertised in discovery).

Each test is decorated ``@pytest.mark.skip(reason="P3.x pending: ...")``
until its implementation chunk lands. The skip markers move chunk by
chunk so progress is visible in CI summaries.
"""

from __future__ import annotations

import pytest

from tests.oidc.helpers import authlib_available


@pytest.mark.oidc
@pytest.mark.slow
@pytest.mark.skipif(not authlib_available(), reason="authlib not installed")
@pytest.mark.skip(reason="P3.1 implementation pending: Keycloak login happy path")
def test_login_callback_pkce_roundtrip(oidc_issuer_url, oidc_client_id) -> None:
    """Authorization Code + PKCE end-to-end against Keycloak.

    Boots a Keycloak realm with one test user, drives the /login flow
    (auth URL with PKCE challenge + state) → exchanges the resulting
    code at /callback → asserts a session cookie was issued + the
    callback redirected to the post-login URL.
    """
    pytest.fail("P3.1 not landed")


@pytest.mark.oidc
@pytest.mark.slow
@pytest.mark.skipif(not authlib_available(), reason="authlib not installed")
@pytest.mark.skip(reason="P3.2 implementation pending: PKCE+state negative tests")
def test_pkce_state_tamper_rejected(oidc_issuer_url, oidc_client_id) -> None:
    """Tampering the ``state`` parameter at /callback must raise.

    No session cookie should be issued, no User row should be created.
    The error response should NOT include the original state value
    (defends against reflected-XSS via error message).
    """
    pytest.fail("P3.2 not landed")


@pytest.mark.oidc
@pytest.mark.slow
@pytest.mark.skipif(not authlib_available(), reason="authlib not installed")
@pytest.mark.skip(reason="P3.3 implementation pending: JIT user provisioning")
def test_jit_provisions_user_and_org_member(oidc_issuer_url, oidc_client_id) -> None:
    """First login from an unknown ``sub`` creates a User + OrganizationMember.

    Subsequent logins reuse the same User row (verified by checking
    ``users.id`` is stable across logins for the same ``sub``).
    The OrganizationMember row's role must match the claim-mapping
    config (default: ``groups`` claim → role).
    """
    pytest.fail("P3.3 not landed")


@pytest.mark.oidc
@pytest.mark.slow
@pytest.mark.skipif(not authlib_available(), reason="authlib not installed")
@pytest.mark.skip(reason="P3.4 implementation pending: userinfo cache + JWT TTL")
def test_userinfo_cache_avoids_per_request_rtt(oidc_issuer_url, oidc_client_id) -> None:
    """N API calls within one refresh window hit the userinfo cache.

    The IdP's ``userinfo`` endpoint must be called ONCE per
    refresh-token lifetime (keyed by ``sub``), not once per API call.
    Verified by counting outbound HTTP calls to the IdP via a recording
    transport.
    """
    pytest.fail("P3.4 not landed")


@pytest.mark.oidc
@pytest.mark.slow
@pytest.mark.skipif(not authlib_available(), reason="authlib not installed")
@pytest.mark.skip(reason="P3.4 implementation pending: revocation within TTL")
def test_user_disabled_in_idp_revoked_within_ttl(oidc_issuer_url, oidc_client_id) -> None:
    """Disabling a user in the IdP revokes access on the next refresh.

    Sequence:
      1. User logs in successfully (access token valid for 15 min).
      2. Admin disables the user in Keycloak.
      3. Within 15 min, the user's next ``userinfo``-backed refresh
         fails (Keycloak returns 401 / `error: invalid_grant`).
      4. The application invalidates the refresh-session row and the
         next API call returns 401.

    Acceptance bound: revocation MUST take effect within the access
    token TTL (15 min). Faster revocation requires back-channel
    logout, which is deferred to v1.1.
    """
    pytest.fail("P3.4 not landed")


@pytest.mark.oidc
@pytest.mark.slow
@pytest.mark.skipif(not authlib_available(), reason="authlib not installed")
@pytest.mark.skip(reason="P3.5 implementation pending: logout flow")
def test_logout_redirects_to_end_session_endpoint(oidc_issuer_url, oidc_client_id) -> None:
    """``POST /api/auth/oidc/logout`` redirects to the IdP's end_session_endpoint.

    Behavior:
      - When the IdP advertises ``end_session_endpoint`` in its OIDC
        discovery document, our /logout endpoint deletes the
        refresh-session row and issues a 302 to that URL with
        ``post_logout_redirect_uri`` set.
      - When the IdP does NOT advertise the endpoint (some legacy
        OAuth-only providers), /logout deletes the session and
        redirects to our own post-logout page (no IdP round-trip).
    """
    pytest.fail("P3.5 not landed")
