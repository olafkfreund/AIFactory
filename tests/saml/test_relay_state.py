"""Tests for HMAC RelayState binding (Epic #35 #41)."""

from __future__ import annotations

import time

import pytest
from server.saml.relay_state import (
    RelayStateInvalid,
    mint,
    verify,
)

SECRET = b"test-secret-key-bytes-32-chars-ok"


def test_round_trip():
    token = mint(SECRET, idp="corp-sso", return_to="https://app.example.com/")
    payload = verify(SECRET, token)
    assert payload.idp == "corp-sso"
    assert payload.return_to == "https://app.example.com/"
    assert payload.nonce  # populated


def test_tampered_payload_rejected():
    token = mint(SECRET, idp="corp-sso", return_to="https://app.example.com/")
    payload_b64, sig = token.split(".", 1)
    # Flip a byte in the payload.
    tampered = payload_b64[:-1] + ("A" if payload_b64[-1] != "A" else "B")
    with pytest.raises(RelayStateInvalid):
        verify(SECRET, f"{tampered}.{sig}")


def test_wrong_secret_rejected():
    token = mint(SECRET, idp="x", return_to="https://app.example.com/")
    with pytest.raises(RelayStateInvalid):
        verify(b"different-secret-key-but-still-32!", token)


def test_expired_token_rejected(monkeypatch):
    token = mint(SECRET, idp="x", return_to="https://app.example.com/")
    # Advance clock past the validity window. Capture the real time
    # function FIRST so the lambda doesn't recurse into itself after
    # monkeypatch swaps time.time.
    real_time = time.time
    future = real_time() + 11 * 60

    # Patch the time function on BOTH the time module and the
    # relay_state module's import (verify() does `import time`
    # at module top, so time.time inside it must also be patched).
    from server.saml import relay_state as rs_mod
    monkeypatch.setattr(rs_mod.time, "time", lambda: future)

    with pytest.raises(RelayStateInvalid):
        verify(SECRET, token)


def test_malformed_token_rejected():
    with pytest.raises(RelayStateInvalid):
        verify(SECRET, "no-dot-here")


def test_missing_required_field_rejected():
    """A token whose payload is missing 'idp' is rejected. We can't
    construct this via mint() — build manually."""
    import base64
    import hashlib
    import hmac
    import json

    bad_payload = json.dumps({"nonce": "x", "exp": int(time.time()) + 60,
                              "return_to": "https://x/"})
    b64 = base64.urlsafe_b64encode(bad_payload.encode()).rstrip(b"=").decode()
    sig = hmac.new(SECRET, b64.encode(), hashlib.sha256).digest()
    sig_b64 = base64.urlsafe_b64encode(sig).rstrip(b"=").decode()
    with pytest.raises(RelayStateInvalid):
        verify(SECRET, f"{b64}.{sig_b64}")
