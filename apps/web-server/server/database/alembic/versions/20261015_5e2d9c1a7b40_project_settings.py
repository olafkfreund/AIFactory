"""add project_settings table (#1795 settings out of the project clone)

Per-project settings used to live in ``<project>/.aifactory/.env``, inside the
clone that task worktrees and agents can write. They now live here.

Adds:

- ``project_settings`` — one row per project. ``project_key`` is the sha256 of
  the project's absolute path; ``settings`` is the sealed JSON settings dict.

Additive: nothing reads the table once the code is reverted.

Revision ID: 5e2d9c1a7b40
Revises: 0400a6b429b0
Create Date: 2026-10-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5e2d9c1a7b40"
down_revision: str | Sequence[str] | None = "0400a6b429b0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "project_settings",
        sa.Column("project_key", sa.String(length=64), primary_key=True),
        sa.Column("settings", sa.Text, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_table("project_settings")
