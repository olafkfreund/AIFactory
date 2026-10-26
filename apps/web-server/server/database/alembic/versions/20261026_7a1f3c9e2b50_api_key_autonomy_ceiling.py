"""add api_keys.autonomy_ceiling (Factory#1960 slice 1)

The most autonomous tier (``low``/``medium``/``hard``) an ``acw_`` key's agent
may act at. Nullable with no default: existing keys record no ceiling, and no
ceiling leaves every merge decision unchanged.

Additive: nothing reads the column once the code is reverted.

Revision ID: 7a1f3c9e2b50
Revises: 5e2d9c1a7b40
Create Date: 2026-10-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7a1f3c9e2b50"
down_revision: str | Sequence[str] | None = "5e2d9c1a7b40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "api_keys",
        sa.Column("autonomy_ceiling", sa.String(length=16), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("api_keys", "autonomy_ceiling")
