"""add oidc_sub column to users

P3.3 of Epic #26 — adds the stable OIDC subject identifier column so
that successive logins for the same IdP user resolve to the same User
row. Nullable so existing locally-registered users (no SSO) are
unaffected; unique so an IdP user can't accidentally fork into two
local accounts.

Revision ID: d8f1a3c5e7b9
Revises: c6e3b2d4a8f0
Create Date: 2026-05-25
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d8f1a3c5e7b9"
down_revision: Union[str, Sequence[str], None] = "c6e3b2d4a8f0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("oidc_sub", sa.String(length=255), nullable=True),
    )
    op.create_unique_constraint(
        "uq_users_oidc_sub", "users", ["oidc_sub"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_users_oidc_sub", "users", type_="unique")
    op.drop_column("users", "oidc_sub")
