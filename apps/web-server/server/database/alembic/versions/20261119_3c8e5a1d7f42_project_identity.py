"""project identity: slug, repo identity, teams (#2157, Factory#3857 phase 1)

Adds to ``projects``: ``slug`` (unique per org), ``repo_identity``, ``team_id``,
``archived_at``; and the ``teams`` and ``team_memberships`` tables. The existing
``projects.settings_json`` now carries the legacy registry entry.

Additive: old code ignores every new column and table.

Revision ID: 3c8e5a1d7f42
Revises: 7a1f3c9e2b50
Create Date: 2026-11-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3c8e5a1d7f42"
down_revision: str | Sequence[str] | None = "7a1f3c9e2b50"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "revision"]


def upgrade() -> None:
    op.create_table(
        "teams",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "org_id", sa.String(36), sa.ForeignKey("organizations.id"), nullable=False
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("slug", sa.String(64), nullable=False),
        sa.Column("is_default", sa.Boolean, nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime, nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("org_id", "slug", name="uq_teams_org_slug"),
    )
    op.create_table(
        "team_memberships",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("team_id", sa.String(36), sa.ForeignKey("teams.id"), nullable=False),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("role", sa.String(50), nullable=False),
        sa.Column("provenance", sa.String(16), nullable=False),
        sa.Column(
            "created_at", sa.DateTime, nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("team_id", "user_id", name="uq_team_memberships_team_user"),
    )
    with op.batch_alter_table("projects") as batch:
        batch.add_column(sa.Column("slug", sa.String(64), nullable=True))
        batch.add_column(sa.Column("repo_identity", sa.String(512), nullable=True))
        batch.add_column(sa.Column("team_id", sa.String(36), nullable=True))
        batch.add_column(sa.Column("archived_at", sa.DateTime, nullable=True))
        batch.create_foreign_key("fk_projects_team_id", "teams", ["team_id"], ["id"])
        batch.create_unique_constraint("uq_projects_org_slug", ["org_id", "slug"])


def downgrade() -> None:
    with op.batch_alter_table("projects") as batch:
        batch.drop_constraint("uq_projects_org_slug", type_="unique")
        batch.drop_constraint("fk_projects_team_id", type_="foreignkey")
        batch.drop_column("archived_at")
        batch.drop_column("team_id")
        batch.drop_column("repo_identity")
        batch.drop_column("slug")
    op.drop_table("team_memberships")
    op.drop_table("teams")
