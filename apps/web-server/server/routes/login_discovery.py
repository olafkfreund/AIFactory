"""IdP discovery endpoint for the AIFactory login page (Epic #35 #41 PR-1b4).

Exposes ``GET /api/auth/identity-providers`` — a public, no-auth endpoint
that returns the list of configured identity providers so the login page can
render the IdP dropdown without hardcoding protocol-specific paths.

This endpoint is intentionally unauthenticated: the login page must fetch it
before any session exists.  The ``TokenAuthMiddleware`` skips auth for the
``/api/auth/`` prefix, so no additional exemption is needed.
"""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter

from ..identity_providers import list_configured_identity_providers

router = APIRouter(tags=["Auth (Identity Providers)"])


@router.get(
    "/api/auth/identity-providers",
    summary="List configured identity providers",
    response_description="Ordered list of IdP descriptors for the login UI",
)
async def list_identity_providers() -> list[dict]:
    """Return every enabled IdP as a JSON array.

    Each object has the fields:
    * ``name``         — stable machine key, e.g. ``"corp-sso"``
    * ``kind``         — ``"oidc"`` or ``"saml"``
    * ``display_name`` — operator-supplied UI label
    * ``login_url``    — path the browser navigates to begin the flow

    An empty array means no SSO is configured; the login page should fall
    back to the local-password form.
    """
    providers = list_configured_identity_providers()
    return [asdict(p) for p in providers]
