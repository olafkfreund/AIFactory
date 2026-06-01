"""Merged identity-provider discovery for the AIFactory login page (Epic #35 #41 PR-1b4).

Exposes a single public function — ``list_configured_identity_providers`` —
that returns every enabled IdP from both the OIDC and SAML subsystems as a
uniform ``IdpDescriptor`` list.  The login page hits
``GET /api/auth/identity-providers`` (see ``routes/login_discovery.py``) and
renders each descriptor as a button or dropdown entry.

Design notes (design doc decision #2)
--------------------------------------
* A single page with an IdP discovery dropdown replaces per-IdP login URLs.
* The existing ``oidc:`` top-level block stays for backward compat — when
  ``APP_OIDC_ENABLED=true`` and the discovery list is empty (no
  ``identityProviders`` YAML stanza), the OIDC IdP surfaces as the "OIDC
  (default)" entry.
* SAML descriptor gets its display name from ``SAML_IDP_DISPLAY_NAME``
  (falling back to ``SAML_IDP_NAME``, then "saml").  This mirrors the
  ``_idp_name()`` helper in ``saml/routes.py``.
* Order: SAML first, then OIDC.  Documented here so callers don't have to
  guess; tests assert it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal


@dataclass
class IdpDescriptor:
    """Describes one configured identity provider for the login UI."""

    # Stable machine-readable key, e.g. "corp-sso" or "oidc".
    name: str
    # Protocol kind — drives which auth flow the browser starts.
    kind: Literal["oidc", "saml"]
    # Human-readable label shown in the dropdown, e.g. "Corp SSO (SAML)".
    display_name: str
    # Absolute path the browser should navigate to to begin the flow.
    login_url: str


def _saml_descriptor() -> IdpDescriptor | None:
    """Return the SAML IdP descriptor when SAML is enabled, else None.

    Reads three env vars so operators can publish a friendly label without
    changing the machine key:
    * SAML_ENABLED        — feature flag (must be "true")
    * SAML_IDP_NAME       — machine-readable key (default "saml")
    * SAML_IDP_DISPLAY_NAME — UI label (default = SAML_IDP_NAME)
    """
    if os.environ.get("SAML_ENABLED", "").lower() != "true":
        return None

    name = os.environ.get("SAML_IDP_NAME", "saml").strip() or "saml"
    display_name = (
        os.environ.get("SAML_IDP_DISPLAY_NAME", "").strip()
        or name
    )
    login_url = f"/api/auth/saml/login?idp={name}"

    return IdpDescriptor(
        name=name,
        kind="saml",
        display_name=display_name,
        login_url=login_url,
    )


def _oidc_descriptor() -> IdpDescriptor | None:
    """Return the OIDC IdP descriptor when OIDC is enabled, else None.

    Intentionally avoids importing the heavy authlib stack — we only need
    the flag check and a display-name env var here.
    """
    # Mirror is_oidc_enabled() without the import chain, so this module
    # stays lightweight and importable even when authlib isn't installed.
    if os.environ.get("APP_OIDC_ENABLED", "").lower() not in {"1", "true", "yes"}:
        return None
    required = ("APP_OIDC_ISSUER_URL", "APP_OIDC_CLIENT_ID", "APP_OIDC_CLIENT_SECRET")
    if not all(os.environ.get(k) for k in required):
        return None

    display_name = (
        os.environ.get("OIDC_DISPLAY_NAME", "").strip()
        or "OIDC (default)"
    )

    return IdpDescriptor(
        name="oidc",
        kind="oidc",
        display_name=display_name,
        login_url="/api/auth/oidc/login",
    )


def list_configured_identity_providers() -> list[IdpDescriptor]:
    """Return every enabled IdP in stable display order: SAML first, OIDC after.

    Returns an empty list when no IdPs are configured — the login page can
    then fall back to showing the local-password form or a "contact your
    admin" message.
    """
    providers: list[IdpDescriptor] = []

    saml = _saml_descriptor()
    if saml is not None:
        providers.append(saml)

    oidc = _oidc_descriptor()
    if oidc is not None:
        providers.append(oidc)

    return providers
