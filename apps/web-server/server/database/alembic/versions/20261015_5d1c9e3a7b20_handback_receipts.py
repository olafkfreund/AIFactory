"""add handback_receipts table (#1696)

Revision ID: 5d1c9e3a7b20
Revises: 0400a6b429b0
Create Date: 2026-10-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5d1c9e3a7b20"
down_revision: str | Sequence[str] | None = "0400a6b429b0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "handback_receipts",
        sa.Column("spec_key", sa.String(length=64), primary_key=True),
        sa.Column("spec_id", sa.String(length=255), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_table("handback_receipts")
