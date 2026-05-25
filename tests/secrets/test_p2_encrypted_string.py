"""P2.1 — EncryptedString TypeDecorator (AES-256-GCM envelope, Fernet root)."""

import pytest


@pytest.mark.secrets
@pytest.mark.skip(reason="P2.1 implementation pending: EncryptedString TypeDecorator")
def test_encrypted_string_roundtrip(fernet_key: str) -> None:
    """Plaintext written via EncryptedString comes back identical on read."""
    pytest.fail("P2.1 not landed")


@pytest.mark.secrets
@pytest.mark.skip(reason="P2.1 implementation pending: AAD validation rejects tampering")
def test_encrypted_string_rejects_tampered_ciphertext(fernet_key: str) -> None:
    """Flipping a single byte in the ciphertext raises InvalidTag."""
    pytest.fail("P2.1 not landed")


@pytest.mark.secrets
@pytest.mark.skip(reason="P2.1 implementation pending: pg_dump shows ciphertext, never plaintext")
def test_pg_dump_contains_no_plaintext_for_encrypted_columns(fernet_key: str) -> None:
    """The raw bytes stored in the column are ciphertext — no plaintext leaks."""
    pytest.fail("P2.1 not landed")
