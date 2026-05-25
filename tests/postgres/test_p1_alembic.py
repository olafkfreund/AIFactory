"""P1.3 / P1.4 — Alembic baseline migration + idempotent upgrade + autoApply modes."""

import os

import pytest

from tests.postgres.helpers import (
    WEB_SERVER_ROOT,
    alembic_available,
    run_alembic,
)


@pytest.mark.postgres
@pytest.mark.slow
@pytest.mark.skip(reason="P1.3 implementation pending: Alembic init + baseline migration")
def test_alembic_config_present() -> None:
    """P1.3 — alembic.ini and versions/ directory exist under apps/web-server/."""
    assert (WEB_SERVER_ROOT / "alembic.ini").exists(), \
        f"{WEB_SERVER_ROOT / 'alembic.ini'} missing"
    assert (WEB_SERVER_ROOT / "server" / "database" / "alembic" / "versions").is_dir(), \
        "Alembic versions/ directory missing"


@pytest.mark.postgres
@pytest.mark.slow
@pytest.mark.skip(reason="P1.3 implementation pending: baseline migration runs on empty Postgres")
def test_alembic_upgrade_head_on_empty_postgres(test_postgres_url: str) -> None:
    """P1.3 — `alembic upgrade head` creates all tables on a fresh Postgres."""
    if not alembic_available():
        pytest.skip("alembic CLI not on PATH")

    result = run_alembic(
        ["upgrade", "head"],
        env={"DATABASE_URL": test_postgres_url},
    )
    assert result.returncode == 0, \
        f"alembic upgrade head failed:\n{result.stderr[-2000:]}"


@pytest.mark.postgres
@pytest.mark.slow
@pytest.mark.skip(reason="P1.3 implementation pending: idempotent upgrade")
def test_alembic_upgrade_idempotent(test_postgres_url: str) -> None:
    """P1.3 — running upgrade head twice is a no-op the second time."""
    if not alembic_available():
        pytest.skip("alembic CLI not on PATH")

    env = {"DATABASE_URL": test_postgres_url}
    first = run_alembic(["upgrade", "head"], env=env)
    assert first.returncode == 0, f"first upgrade failed: {first.stderr[-1000:]}"

    second = run_alembic(["upgrade", "head"], env=env)
    assert second.returncode == 0, \
        f"second upgrade was not idempotent: {second.stderr[-1000:]}"


@pytest.mark.postgres
@pytest.mark.slow
@pytest.mark.skip(reason="P1.4 implementation pending: autoApply=true boot-applies migrations")
def test_app_boot_with_autoapply_true_runs_migrations(test_postgres_url: str) -> None:
    """P1.4 — `migrations.autoApply=true` causes app boot to upgrade DB schema."""
    # Will exercise the boot-time migration path via a subprocess launch of
    # `python -m server.main` against an empty Postgres + autoApply=true.
    # Test body lands with P1.4.
    pytest.fail("P1.4 implementation not landed yet")


@pytest.mark.postgres
@pytest.mark.slow
@pytest.mark.skip(reason="P1.4 implementation pending: autoApply=false fails fast on un-migrated DB")
def test_app_boot_with_autoapply_false_fails_fast(test_postgres_url: str) -> None:
    """P1.4 — `migrations.autoApply=false` against an un-migrated DB fails fast."""
    pytest.fail("P1.4 implementation not landed yet")


@pytest.mark.postgres
@pytest.mark.slow
@pytest.mark.skip(reason="P1.5 implementation pending: app-side UUIDs + no CREATE EXTENSION")
def test_alembic_succeeds_without_create_extension_privilege(test_postgres_url: str) -> None:
    """P1.5 — Alembic upgrade succeeds against a role with NO CREATE EXTENSION grant.

    Mirrors the bank scenario: app role gets DML + schema ownership but cannot
    install pgcrypto/uuid-ossp. UUIDs must therefore be generated app-side.
    """
    if not alembic_available():
        pytest.skip("alembic CLI not on PATH")

    # Implementation note (P1.5): this test runs the migration as a role that
    # lacks CREATE EXTENSION. The test setup creates such a role + grants only
    # the minimum privileges from guides/deployment/postgres-privileges.md.
    pytest.fail("P1.5 setup not landed yet")
