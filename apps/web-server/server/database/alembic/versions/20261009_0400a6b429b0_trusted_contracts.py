"""add trusted_contracts table (#1667 signed-contract merge gate)

The merge gate used to read ``context/task_contract.json`` from the spec
directory, which the coding agent can edit or delete. The signed contract is now
stored at ingest in this table and the gate decides from it.

Adds:

- ``trusted_contracts`` — one row per trusted spec directory. ``spec_key`` is
  the sha256 of the resolved spec path; ``contract`` is the verbatim signed
  plan; ``build_isolation`` is stamped by the web server at spawn.

Additive: nothing reads the table once the code is reverted.

Revision ID: 0400a6b429b0
Revises: c1f5a3d7b924
Create Date: 2026-10-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0400a6b429b0"
down_revision: str | Sequence[str] | None = "c1f5a3d7b924"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "trusted_contracts",
        sa.Column("spec_key", sa.String(length=64), primary_key=True),
        sa.Column("spec_id", sa.String(length=255), nullable=False),
        sa.Column("contract", sa.Text, nullable=False),
        sa.Column("build_isolation", sa.String(length=16), nullable=True),
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
    op.drop_table("trusted_contracts")
