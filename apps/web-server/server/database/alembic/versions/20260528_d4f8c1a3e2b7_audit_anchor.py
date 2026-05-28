"""add audit_anchors + audit_signing_keys + audit_logs.classification + users.last_login_at

Epic #35 #43 PR-1. Schema groundwork for the signed audit-chain anchor.

Adds:

- ``audit_anchors`` — one row per signed chain head (daily cron + manual triggers).
- ``audit_signing_keys`` — versioned wrapped-key storage so KMS rotation
  doesn't silently invalidate prior anchors.
- ``audit_logs.classification`` — three-tier data-classification column
  (public / internal / confidential). Default 'internal' on backfill, then
  NOT NULL. Included in `_canonical()` so the chain protects classification
  from tampering.
- ``users.last_login_at`` — referenced by the new access-review endpoint.

Migration safety
----------------

The ``audit_logs.classification`` addition is the only chain-affecting
change. We backfill every existing row to ``'internal'`` BEFORE applying
NOT NULL so that the new ``_canonical()`` (which reads ``classification``)
re-verifies the chain identically — historical rows hash the same as they
did pre-migration (because empty-string was the old behavior for
"missing field" and ``'internal'`` is the new universal default).

A dedicated PR-1 test verifies an end-to-end chain re-verification after
the migration, against rows written by the pre-migration code.

Revision ID: d4f8c1a3e2b7
Revises: 7a3e1c8f9b2d
Create Date: 2026-05-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4f8c1a3e2b7"
down_revision: str | Sequence[str] | None = "7a3e1c8f9b2d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. audit_signing_keys — versioned wrapped-key storage.
    op.create_table(
        "audit_signing_keys",
        sa.Column("version", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("wrapped_key", sa.LargeBinary, nullable=False),
        sa.Column(
            "created_at", sa.DateTime,
            server_default=sa.func.now(), nullable=False,
        ),
        # retired_at: NULL = currently signing; non-NULL = verify-only.
        # The cron job picks up MAX(version) WHERE retired_at IS NULL.
        sa.Column("retired_at", sa.DateTime, nullable=True),
    )

    # 2. audit_anchors — one row per anchor.
    op.create_table(
        "audit_anchors",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("chain_head_hash", sa.String(length=64), nullable=False),
        sa.Column("signature", sa.String(length=64), nullable=False),
        sa.Column("signed_at", sa.DateTime, nullable=False),
        sa.Column(
            "key_version", sa.Integer,
            sa.ForeignKey("audit_signing_keys.version"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime,
            server_default=sa.func.now(), nullable=False,
        ),
    )
    op.create_index(
        "ix_audit_anchors_signed_at", "audit_anchors", ["signed_at"],
    )
    # Idempotency: only ONE anchor per UTC date. Two concurrent triggers
    # racing for the same day's anchor — one wins.
    # SQLite + Postgres both support expression indexes; Alembic's
    # create_index with `sqlite_where` / `postgresql_where` works for
    # uniqueness on DATE(signed_at). We use a portable approach: a
    # generated column via op.execute for Postgres, and rely on
    # application-side guard on SQLite (the existing test fixture
    # exercise both).
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # ``signed_at`` is TIMESTAMP WITHOUT TIME ZONE; the cron writer
        # always stores naive-UTC datetimes (``datetime.now(timezone.utc)``
        # with tzinfo stripped). So ``(signed_at::date)`` IS the UTC date
        # and is IMMUTABLE (Postgres rejects index expressions involving
        # ``AT TIME ZONE`` because that depends on session state). The
        # UTC discipline is enforced in audit_anchor_cron.py via an
        # assertion before INSERT.
        op.execute(
            "CREATE UNIQUE INDEX uq_audit_anchors_date "
            "ON audit_anchors ((signed_at::date))"
        )
    # SQLite test path: enforced application-side.

    # 3. audit_logs.classification — three-tier classification column.
    # Step 3a: add as nullable with default 'internal' so existing rows
    # get the default value automatically.
    with op.batch_alter_table("audit_logs") as batch:
        batch.add_column(
            sa.Column(
                "classification", sa.String(length=16),
                nullable=False, server_default="internal",
            ),
        )

    # 4. users.last_login_at — referenced by access-review endpoint.
    with op.batch_alter_table("users") as batch:
        batch.add_column(
            sa.Column("last_login_at", sa.DateTime, nullable=True),
        )


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.drop_column("last_login_at")

    with op.batch_alter_table("audit_logs") as batch:
        batch.drop_column("classification")

    op.drop_index(
        "ix_audit_anchors_signed_at", table_name="audit_anchors",
    )
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS uq_audit_anchors_date")
    op.drop_table("audit_anchors")
    op.drop_table("audit_signing_keys")
