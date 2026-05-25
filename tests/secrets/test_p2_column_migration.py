"""P2.3 — credential-column migration + plaintext backfill."""

import pytest


@pytest.mark.secrets
@pytest.mark.slow
@pytest.mark.skip(reason="P2.3 implementation pending: Alembic backfill migration")
def test_migration_backfills_plaintext_to_encrypted() -> None:
    """Seed a pre-migration DB with plaintext credentials; run upgrade head;
    confirm the column now stores ciphertext and decrypts back to plaintext."""
    pytest.fail("P2.3 not landed")


@pytest.mark.secrets
@pytest.mark.slow
@pytest.mark.skip(reason="P2.3 implementation pending: pg_dump validation")
def test_pg_dump_contains_no_plaintext_credentials() -> None:
    """After migration, `pg_dump` of integration_tokens / settings / oauth_tokens
    contains no plaintext credentials anywhere in the dump output."""
    pytest.fail("P2.3 not landed")
