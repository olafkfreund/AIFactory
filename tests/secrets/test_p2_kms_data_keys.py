"""P2.2 — kms_data_keys table + per-org data key generation + cache."""

import pytest


@pytest.mark.secrets
@pytest.mark.skip(reason="P2.2 implementation pending: data key created lazily on first encrypt")
def test_kms_data_key_created_on_first_use() -> None:
    """First EncryptedString write for an org creates exactly one kms_data_keys row."""
    pytest.fail("P2.2 not landed")


@pytest.mark.secrets
@pytest.mark.skip(reason="P2.2 implementation pending: LRU cache evicts on rotated_at change")
def test_lru_cache_evicts_on_rotation() -> None:
    """When kms_data_keys.rotated_at changes, the cached unwrapped key is dropped."""
    pytest.fail("P2.2 not landed")


@pytest.mark.secrets
@pytest.mark.skip(reason="P2.2 implementation pending: per-org data key isolation")
def test_data_key_isolation_between_orgs() -> None:
    """Org A's ciphertext can NOT be decrypted with Org B's data key."""
    pytest.fail("P2.2 not landed")
