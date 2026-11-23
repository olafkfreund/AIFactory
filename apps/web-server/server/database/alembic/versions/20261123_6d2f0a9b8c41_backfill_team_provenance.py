"""Mark OIDC default-team grants created by startup backfill (#2198)."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "6d2f0a9b8c41"
down_revision: str | Sequence[str] | None = "9c4e2a6d1b83"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        sa.text("""
        UPDATE team_memberships SET provenance = 'backfill'
        WHERE provenance = 'manual'
          AND EXISTS (
            SELECT 1 FROM teams t
            JOIN org_members om ON om.org_id = t.org_id
            JOIN users u ON u.id = om.user_id
            WHERE t.id = team_memberships.team_id
              AND t.is_default = true
              AND om.user_id = team_memberships.user_id
              AND om.role = team_memberships.role
              AND u.oidc_sub IS NOT NULL
          )
    """)
    )


def downgrade() -> None:
    op.execute(
        sa.text("""
        UPDATE team_memberships SET provenance = 'manual'
        WHERE provenance = 'backfill'
    """)
    )
