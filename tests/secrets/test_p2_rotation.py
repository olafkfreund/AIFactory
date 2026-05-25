"""P2.5 — KMS root-key rotation re-wraps per-org data keys."""

import pytest


@pytest.mark.secrets
@pytest.mark.slow
@pytest.mark.skip(reason="P2.5 implementation pending: rotation re-wraps all keys")
def test_rotation_rewraps_all_per_org_keys() -> None:
    """`rotate_kms_root.py` re-wraps every kms_data_keys row under the new root
    and bumps rotated_at; plaintext-equivalent decrypt still works."""
    pytest.fail("P2.5 not landed")


@pytest.mark.secrets
@pytest.mark.slow
@pytest.mark.skip(reason="P2.5 implementation pending: rotation invalidates in-process cache")
def test_rotation_invalidates_in_process_cache() -> None:
    """In-process LRU cache notices the rotated_at change and re-unwraps from KMS."""
    pytest.fail("P2.5 not landed")
