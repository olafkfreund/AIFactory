"""add scim_groups + scim_group_members tables for SCIM 2.0 Groups

Epic #35 #41 PR-1b3. Design decision #5 locks SCIM Groups as a
parallel concept for v1.1 (not mapped to the existing org/member
model). Integration with tenant isolation lands in Epic #36.

Soft-delete pattern mirrors the User active flag: DELETE on
/scim/v2/Groups sets active=False; GET returns 404 so Azure AD's
sync sees the group as gone without losing audit history.

batch_alter_table is used throughout for SQLite compat (SQLite
doesn't support ADD CONSTRAINT on existing tables; batch mode
recreates the table internally). The Postgres path executes the
same DDL directly.

Revision ID: a1b2c3d4e5f6
Revises: f9c2b1a4d8e7
Create Date: 2026-05-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: str | Sequence[str] | None = "f9c2b1a4d8e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "scim_groups",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("external_id", sa.String(length=512), nullable=True, unique=True),
        sa.Column("active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at",
            sa.DateTime,
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime,
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_scim_groups_external_id", "scim_groups", ["external_id"])
    op.create_index("ix_scim_groups_active", "scim_groups", ["active"])

    op.create_table(
        "scim_group_members",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "group_id",
            sa.String(length=36),
            sa.ForeignKey("scim_groups.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("display", sa.String(length=255), nullable=True),
        sa.UniqueConstraint(
            "group_id", "user_id", name="uq_scim_group_members_group_user"
        ),
    )
    op.create_index("ix_scim_group_members_user_id", "scim_group_members", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_scim_group_members_user_id", table_name="scim_group_members")
    op.drop_table("scim_group_members")
    op.drop_index("ix_scim_groups_active", table_name="scim_groups")
    op.drop_index("ix_scim_groups_external_id", table_name="scim_groups")
    op.drop_table("scim_groups")
