"""Audit-chain anchor signer + verifier (Epic #35 #43 PR-1b).

What this module does
---------------------

Once per day the cron in ``server/jobs/audit_anchor_cron.py`` reads
the current chain head from ``audit_logs.prev_hash`` and asks this
module to sign it. The signature is HMAC-SHA256 with a 32-byte
symmetric key wrapped by the existing KMS backend
(``crypto/kms/``). Wrapped keys are versioned in
``audit_signing_keys`` so KMS root-key rotation doesn't invalidate
prior anchors — verifiers always load the wrapped key matching the
anchor's ``key_version`` and unwrap via the current KMS root.

Why HMAC and not asymmetric
---------------------------

The v1.1 verifier is the operator with KMS access. Public verifiers
arrive in v1.2 with external publication (S3 WORM / RFC 3161 / Sigstore).
HMAC reuses the existing KMS abstraction's encrypt/decrypt without
needing a separate ``sign()`` method on every backend.

Operational safety
------------------

The unwrapped key is held in a ``_SigningKey`` newtype whose
``__repr__`` never serialises the bytes — defence against accidental
logging. Tests assert no logger call ever emits the raw key.
"""

from __future__ import annotations

import hmac
import logging
import os
import secrets
from dataclasses import dataclass
from hashlib import sha256

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..crypto.kms import get_backend
from ..database.models import AuditSigningKey

logger = logging.getLogger(__name__)

#: HMAC + ed25519 + Fernet all want 32-byte keys; the cron generator
#: produces exactly this length.
_KEY_LENGTH_BYTES = 32


@dataclass(frozen=True)
class _SigningKey:
    """Wraps a 32-byte HMAC-SHA256 key with leak-safe ``__repr__``.

    The class is intentionally minimal — no methods that could be
    overridden to print the bytes, no ``__str__`` either (Python
    falls back to ``__repr__`` which is safe).

    Tests assert:
      - ``repr(key)`` does NOT contain any byte from ``key.raw``.
      - ``str(key)`` does NOT either.
    """

    raw: bytes
    version: int

    def __repr__(self) -> str:
        # NEVER include the raw bytes. Length + version is enough for
        # operational debugging.
        return f"<SigningKey v={self.version} len={len(self.raw)}>"


# ---------------------------------------------------------------------------
# Key generation + KMS lifecycle
# ---------------------------------------------------------------------------


def generate_new_key() -> bytes:
    """Generate a fresh 32-byte HMAC key with cryptographic randomness."""
    return secrets.token_bytes(_KEY_LENGTH_BYTES)


async def ensure_active_key(db: AsyncSession) -> _SigningKey:
    """Return the currently-active signing key, generating one if none exist.

    "Active" = the highest-version row in ``audit_signing_keys`` whose
    ``retired_at`` is NULL.

    Called by:
      - the cron at the start of every signing window
      - the verifier when checking an anchor's signature (which then
        loads the SPECIFIC key_version from the anchor, not the active one)
    """
    stmt = (
        select(AuditSigningKey)
        .where(AuditSigningKey.retired_at.is_(None))
        .order_by(AuditSigningKey.version.desc())
        .limit(1)
    )
    result = await db.execute(stmt)
    row = result.scalar_one_or_none()
    if row is not None:
        return _unwrap(row)

    # Bootstrap: no key yet. Generate one + wrap + persist.
    raw = generate_new_key()
    kms = get_backend()
    wrapped = kms.encrypt(raw)
    new_row = AuditSigningKey(wrapped_key=wrapped)
    db.add(new_row)
    await db.flush()
    logger.info(
        "audit anchor: bootstrapped signing key v%d (no prior key existed)",
        new_row.version,
    )
    return _SigningKey(raw=raw, version=new_row.version)


async def load_key_by_version(
    db: AsyncSession, version: int,
) -> _SigningKey:
    """Load + unwrap a specific historical key. Used by the verifier
    when checking an anchor's signature."""
    stmt = select(AuditSigningKey).where(AuditSigningKey.version == version)
    result = await db.execute(stmt)
    row = result.scalar_one_or_none()
    if row is None:
        raise SigningKeyMissingError(
            f"no audit_signing_keys row for version {version}; cannot verify"
        )
    return _unwrap(row)


def _unwrap(row: AuditSigningKey) -> _SigningKey:
    """Unwrap a stored wrapped_key via the current KMS root, asserting
    the unwrapped result is exactly 32 raw bytes.

    The 32-byte assertion is the design's reviewer-finding #4 — every
    KMS backend's ``decrypt()`` MUST return raw bytes of the expected
    length, never a higher-level envelope or string. A mismatch
    silently produces wrong HMACs on the cloud backends.
    """
    kms = get_backend()
    raw = kms.decrypt(row.wrapped_key)
    if not isinstance(raw, bytes):
        raise SigningKeyCorruptError(
            f"KMS backend decrypt() returned {type(raw).__name__}, expected bytes"
        )
    if len(raw) != _KEY_LENGTH_BYTES:
        raise SigningKeyCorruptError(
            f"unwrapped key for v{row.version} is "
            f"{len(raw)} bytes, expected {_KEY_LENGTH_BYTES}"
        )
    return _SigningKey(raw=raw, version=row.version)


# ---------------------------------------------------------------------------
# Sign + verify
# ---------------------------------------------------------------------------


def sign_anchor(chain_head: str, key: _SigningKey) -> str:
    """Return hex HMAC-SHA256(key, chain_head).

    ``chain_head`` is the hex SHA-256 string from
    ``audit_logs.prev_hash`` of the last row covered by this anchor,
    or the empty string for an anchor signed when no rows exist
    (treated equivalently to GENESIS).
    """
    return hmac.new(key.raw, chain_head.encode("utf-8"), sha256).hexdigest()


def verify_anchor(
    chain_head: str, signature_hex: str, key: _SigningKey,
) -> bool:
    """Constant-time HMAC verify. Returns True if the signature is valid."""
    expected = sign_anchor(chain_head, key)
    return hmac.compare_digest(expected, signature_hex)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SigningKeyMissingError(RuntimeError):
    """The audit_signing_keys row for the requested version doesn't exist."""


class SigningKeyCorruptError(RuntimeError):
    """KMS decrypt() returned something other than 32 raw bytes."""


# ---------------------------------------------------------------------------
# Module-level constants exposed for tests
# ---------------------------------------------------------------------------


KEY_LENGTH_BYTES = _KEY_LENGTH_BYTES
# The genesis sentinel used by the chain when no prior rows exist.
# Anchors signed before any audit rows are emitted use this value.
GENESIS_CHAIN_HEAD = os.environ.get(
    "AUDIT_ANCHOR_GENESIS", "GENESIS",
)
