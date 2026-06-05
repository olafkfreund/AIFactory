"""oidc_refresh_sessions.idp_refresh_token — real per-user revocation (#366)

Stores the IdP-issued refresh token (offline_access scope) so the OIDC refresh
path can perform a real per-user revocation check (refresh-token grant against
the IdP) instead of a discovery-liveness probe. Encrypted at rest via the
EncryptedString TypeDecorator (impl = LargeBinary). Nullable: legacy rows and
IdPs that don't issue a refresh token fall back to the liveness probe.

Revision ID: f1a2b3c4d5e6
Revises: c3d7e8f1a2b4
Create Date: 2026-06-05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f1a2b3c4d5e6"
down_revision: str | Sequence[str] | None = "c3d7e8f1a2b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("oidc_refresh_sessions") as batch:
        batch.add_column(
            sa.Column("idp_refresh_token", sa.LargeBinary(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("oidc_refresh_sessions") as batch:
        batch.drop_column("idp_refresh_token")
