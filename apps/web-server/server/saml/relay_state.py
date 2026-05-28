"""HMAC-signed RelayState binding for SAML SP-init CSRF defence.

Why
---

The SAML RelayState parameter is round-tripped through the IdP back
to our /saml/acs endpoint. Without a binding we can't tell whether
the assertion we receive corresponds to a login WE initiated.

We embed:
  - nonce: 16 bytes of randomness, prevents replay
  - exp: unix-time expiry (now + 10 min)
  - return_to: validated absolute URL on our origin
  - idp: which IdP this login was for (must match issuer on /acs)

The whole payload is signed with HMAC-SHA256 using the existing
FastAPI session secret. On /acs we verify the HMAC, check expiry,
and confirm the issuer matches the embedded idp name.

This makes the design's decision #9 ("RelayState binding") concrete.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass

_VALIDITY_SECONDS = 10 * 60  # 10 minutes — generous for slow user flows


@dataclass(frozen=True)
class RelayPayload:
    """Decoded RelayState contents."""

    nonce: str
    exp: int
    return_to: str
    idp: str


def mint(secret_key: bytes, idp: str, return_to: str) -> str:
    """Issue a signed RelayState token. Caller passes ``return_to``
    AFTER same-origin validation."""
    payload = {
        "nonce": secrets.token_urlsafe(16),
        "exp": int(time.time()) + _VALIDITY_SECONDS,
        "return_to": return_to,
        "idp": idp,
    }
    payload_b64 = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(secret_key, payload_b64.encode(), hashlib.sha256).digest()
    return f"{payload_b64}.{_b64url(sig)}"


def verify(secret_key: bytes, token: str) -> RelayPayload:
    """Validate the token + return its payload.

    Raises ``RelayStateInvalid`` on any tamper / expiry / format error.
    Constant-time HMAC comparison."""
    try:
        payload_b64, sig_b64 = token.split(".", 1)
    except ValueError as exc:
        raise RelayStateInvalid("malformed RelayState (missing signature)") from exc

    expected_sig = hmac.new(
        secret_key, payload_b64.encode(), hashlib.sha256,
    ).digest()
    try:
        actual_sig = _b64url_decode(sig_b64)
    except ValueError as exc:
        raise RelayStateInvalid("malformed RelayState (bad signature b64)") from exc

    if not hmac.compare_digest(expected_sig, actual_sig):
        raise RelayStateInvalid("RelayState signature mismatch")

    try:
        payload_json = _b64url_decode(payload_b64).decode()
        payload = json.loads(payload_json)
    except (ValueError, json.JSONDecodeError) as exc:
        raise RelayStateInvalid("malformed RelayState (bad payload)") from exc

    required = {"nonce", "exp", "return_to", "idp"}
    if not required.issubset(payload):
        raise RelayStateInvalid("RelayState missing required fields")

    if int(payload["exp"]) <= int(time.time()):
        raise RelayStateInvalid("RelayState expired")

    return RelayPayload(
        nonce=payload["nonce"],
        exp=int(payload["exp"]),
        return_to=payload["return_to"],
        idp=payload["idp"],
    )


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(data: str) -> bytes:
    # urlsafe_b64decode requires padding; add it back.
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


class RelayStateInvalid(ValueError):
    """RelayState failed signature / expiry / format validation."""
