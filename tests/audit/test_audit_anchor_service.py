"""Tests for the audit_anchor.py signer/verifier service (#43 PR-1b).

Covers:
- ``_SigningKey.__repr__`` never leaks the raw bytes.
- ``generate_new_key()`` returns 32 random bytes.
- ``ensure_active_key()`` bootstraps the first key when none exist;
  on subsequent calls returns the same key.
- ``ensure_active_key()`` picks the highest-version non-retired row.
- ``load_key_by_version()`` round-trips a stored key.
- ``load_key_by_version()`` raises on a missing version.
- KMS-decrypt contract: when the backend returns wrong length or
  wrong type, we raise rather than silently sign wrong HMACs.
- ``sign_anchor`` is deterministic + matches a Python-reference HMAC.
- ``verify_anchor`` accepts a fresh signature + rejects a tampered one.
- Log-safety: no logger call within this module ever emits the raw
  key bytes (even via repr leakage in higher-level logging).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import sys
from pathlib import Path

import pytest

_WEB_SERVER = Path(__file__).parent.parent.parent / "apps" / "web-server"
if str(_WEB_SERVER) not in sys.path:
    sys.path.insert(0, str(_WEB_SERVER))

from datetime import UTC

from server.crypto import kms as kms_mod
from server.services import audit_anchor as svc


def _run(coro):
    """Run an async coroutine synchronously in the test body."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# _SigningKey leak safety
# ---------------------------------------------------------------------------


def test_signing_key_repr_never_leaks_bytes():
    """The whole point of _SigningKey: repr() omits the raw bytes."""
    raw = b"\x00" * 31 + b"\xff"  # distinctive byte
    key = svc._SigningKey(raw=raw, version=7)
    repr_str = repr(key)
    str_str = str(key)
    # Distinctive byte 0xff must not appear in repr (it'd suggest the
    # bytes leaked through some hex/representation path).
    assert "\\xff" not in repr_str
    assert "\\xff" not in str_str
    # Version + length OK to expose; bytes never.
    assert "v=7" in repr_str
    assert "len=32" in repr_str


def test_signing_key_repr_logged_safely(caplog):
    """A logger call that includes a _SigningKey instance via %s / %r
    only emits the safe repr, never the raw bytes."""
    raw = bytes(range(32))  # 0x00..0x1f
    key = svc._SigningKey(raw=raw, version=99)

    logger = logging.getLogger("test_signing_key_repr_logged_safely")
    with caplog.at_level("INFO"):
        logger.info("here is the key: %r and as str: %s", key, key)
        logger.info("plain interp: %s", key)

    # No log record's message OR repr ever leaks the bytes.
    for rec in caplog.records:
        msg = rec.getMessage()
        # 0x1f is the last byte; if any consecutive 0x00-0x1f range
        # leaks through it'd hint that someone called .raw or similar.
        for b in range(32):
            assert chr(b) not in msg, f"byte 0x{b:02x} leaked: {msg!r}"


# ---------------------------------------------------------------------------
# Key generation
# ---------------------------------------------------------------------------


def test_generate_new_key_length_and_randomness():
    k1 = svc.generate_new_key()
    k2 = svc.generate_new_key()
    assert len(k1) == svc.KEY_LENGTH_BYTES == 32
    assert len(k2) == 32
    # Two consecutive calls return different bytes (probabilistic; 32
    # random bytes colliding is astronomically unlikely).
    assert k1 != k2


# ---------------------------------------------------------------------------
# ensure_active_key + load_key_by_version
# ---------------------------------------------------------------------------


def test_ensure_active_key_bootstraps_when_empty(fresh_db, monkeypatch):
    """First call generates a new key + persists wrapped."""
    monkeypatch.setenv("KMS_BACKEND", "fernet")
    monkeypatch.setenv("KMS_FERNET_KEY", _fernet_test_key())
    kms_mod.reset_backend_cache()

    _engine, SessionLocal = fresh_db

    async def _run_test():
        async with SessionLocal() as db:
            key = await svc.ensure_active_key(db)
            await db.commit()
            return key

    key = _run(_run_test())
    assert key.version == 1
    assert len(key.raw) == 32


def test_ensure_active_key_returns_existing_on_second_call(
    fresh_db,
    monkeypatch,
):
    """Calling twice returns the same key, doesn't create a second row."""
    monkeypatch.setenv("KMS_BACKEND", "fernet")
    monkeypatch.setenv("KMS_FERNET_KEY", _fernet_test_key())
    kms_mod.reset_backend_cache()

    _engine, SessionLocal = fresh_db

    async def _run_test():
        async with SessionLocal() as db:
            k1 = await svc.ensure_active_key(db)
            await db.commit()
        async with SessionLocal() as db:
            k2 = await svc.ensure_active_key(db)
        return k1, k2

    k1, k2 = _run(_run_test())
    assert k1.version == k2.version == 1
    assert k1.raw == k2.raw


def test_ensure_active_key_picks_highest_unretired(fresh_db, monkeypatch):
    """When v1 is retired and v2 is active, return v2."""
    monkeypatch.setenv("KMS_BACKEND", "fernet")
    monkeypatch.setenv("KMS_FERNET_KEY", _fernet_test_key())
    kms_mod.reset_backend_cache()

    from datetime import datetime

    from server.database.models import AuditSigningKey

    _engine, SessionLocal = fresh_db

    async def _run_test():
        # Manually create v1 retired + v2 active.
        kms = kms_mod.get_backend()
        async with SessionLocal() as db:
            v1 = AuditSigningKey(
                wrapped_key=kms.encrypt(b"\x01" * 32),
                retired_at=datetime.now(UTC),
            )
            v2 = AuditSigningKey(
                wrapped_key=kms.encrypt(b"\x02" * 32),
            )
            db.add(v1)
            db.add(v2)
            await db.commit()

        async with SessionLocal() as db:
            active = await svc.ensure_active_key(db)
            return active

    active = _run(_run_test())
    assert active.version == 2
    assert active.raw == b"\x02" * 32


def test_load_key_by_version_round_trip(fresh_db, monkeypatch):
    monkeypatch.setenv("KMS_BACKEND", "fernet")
    monkeypatch.setenv("KMS_FERNET_KEY", _fernet_test_key())
    kms_mod.reset_backend_cache()

    _engine, SessionLocal = fresh_db

    async def _run_test():
        async with SessionLocal() as db:
            k = await svc.ensure_active_key(db)
            await db.commit()
        async with SessionLocal() as db:
            return await svc.load_key_by_version(db, k.version)

    k = _run(_run_test())
    assert k.version == 1
    assert len(k.raw) == 32


def test_load_key_missing_version_raises(fresh_db, monkeypatch):
    monkeypatch.setenv("KMS_BACKEND", "fernet")
    monkeypatch.setenv("KMS_FERNET_KEY", _fernet_test_key())
    kms_mod.reset_backend_cache()

    _engine, SessionLocal = fresh_db

    async def _run_test():
        async with SessionLocal() as db:
            await svc.load_key_by_version(db, 999)

    with pytest.raises(svc.SigningKeyMissingError):
        _run(_run_test())


# ---------------------------------------------------------------------------
# KMS contract enforcement
# ---------------------------------------------------------------------------


def test_unwrap_rejects_non_bytes(monkeypatch):
    """Reviewer finding #4: if a KMS backend's decrypt returns the
    wrong type, sign_anchor would silently fail. We raise instead."""
    from server.database.models import AuditSigningKey

    class _BadBackend:
        def encrypt(self, b: bytes) -> bytes:
            return b

        def decrypt(self, b: bytes):
            return "not-bytes"

    # Patch the import the service captured at import time.
    monkeypatch.setattr(svc, "get_backend", lambda: _BadBackend())

    row = AuditSigningKey(wrapped_key=b"\x00" * 32)
    row.version = 1

    with pytest.raises(svc.SigningKeyCorruptError, match="returned str"):
        svc._unwrap(row)


def test_unwrap_rejects_wrong_length(monkeypatch):
    """A 16-byte decrypted result would be a valid HMAC key SDK-wise
    but breaks our 32-byte invariant."""
    from server.database.models import AuditSigningKey

    class _ShortBackend:
        def encrypt(self, b: bytes) -> bytes:
            return b

        def decrypt(self, b: bytes) -> bytes:
            return b"\x00" * 16

    monkeypatch.setattr(svc, "get_backend", lambda: _ShortBackend())

    row = AuditSigningKey(wrapped_key=b"\x00" * 32)
    row.version = 5

    with pytest.raises(svc.SigningKeyCorruptError, match="16 bytes"):
        svc._unwrap(row)


# ---------------------------------------------------------------------------
# Sign + verify
# ---------------------------------------------------------------------------


def test_sign_anchor_deterministic():
    raw = b"\x42" * 32
    key = svc._SigningKey(raw=raw, version=1)
    s1 = svc.sign_anchor("abc123", key)
    s2 = svc.sign_anchor("abc123", key)
    assert s1 == s2
    # Sanity: matches the reference Python HMAC.
    expected = hmac.new(raw, b"abc123", hashlib.sha256).hexdigest()
    assert s1 == expected


def test_sign_anchor_differs_for_different_inputs():
    key = svc._SigningKey(raw=b"\x00" * 32, version=1)
    assert svc.sign_anchor("a", key) != svc.sign_anchor("b", key)


def test_verify_accepts_fresh_signature():
    key = svc._SigningKey(raw=b"\x77" * 32, version=1)
    sig = svc.sign_anchor("head", key)
    assert svc.verify_anchor("head", sig, key) is True


def test_verify_rejects_tampered_signature():
    key = svc._SigningKey(raw=b"\x77" * 32, version=1)
    sig = svc.sign_anchor("head", key)
    # Flip one hex char.
    tampered = ("a" if sig[0] != "a" else "b") + sig[1:]
    assert svc.verify_anchor("head", tampered, key) is False


def test_verify_rejects_wrong_chain_head():
    key = svc._SigningKey(raw=b"\x77" * 32, version=1)
    sig = svc.sign_anchor("head", key)
    assert svc.verify_anchor("different-head", sig, key) is False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fernet_test_key() -> str:
    """A deterministic Fernet key for the unit test path. Real deploys
    set this via env from a Secret; tests just need any valid 32-byte
    base64 key."""
    import base64

    return base64.urlsafe_b64encode(b"\xaa" * 32).decode()
