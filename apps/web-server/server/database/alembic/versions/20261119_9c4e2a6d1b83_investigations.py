"""add investigations + investigation_logs (#2156 read-only investigate run kind)

Adds:

- ``investigations`` — one row per read-only investigation run. A partial
  unique index on ``project_id WHERE pods_gone = false`` is the per-project
  slot: a run holds it until its pods are confirmed gone.
- ``investigation_logs`` — scrubbed log lines, ``(run_id, seq)`` indexed.

Additive: nothing reads either table once the code is reverted.

Revision ID: 9c4e2a6d1b83
Revises: 7a1f3c9e2b50
Create Date: 2026-11-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9c4e2a6d1b83"
down_revision: str | Sequence[str] | None = "7a1f3c9e2b50"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "investigations",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("repo", sa.String(length=1024), nullable=False),
        sa.Column("sha", sa.String(length=40), nullable=False),
        sa.Column("request", sa.String(length=2000), nullable=False),
        sa.Column("test_command", sa.Text, nullable=True),
        sa.Column("limits", sa.JSON, nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("pending_state", sa.String(length=16), nullable=True),
        sa.Column("reason", sa.Text, nullable=True),
        sa.Column("lease_expires_at", sa.DateTime, nullable=False),
        sa.Column(
            "pods_gone", sa.Boolean, nullable=False, server_default=sa.false()
        ),
        sa.Column("stopping_since", sa.DateTime, nullable=True),
        sa.Column("agent_job", sa.String(length=253), nullable=True),
        sa.Column("test_jobs", sa.JSON, nullable=False, server_default="[]"),
        sa.Column("run_tests_used", sa.Integer, nullable=False, server_default="0"),
        sa.Column("tokens_used", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("turns_used", sa.Integer, nullable=False, server_default="0"),
        sa.Column("tests", sa.JSON, nullable=True),
        sa.Column("report", sa.JSON, nullable=True),
        sa.Column(
            "scrub_open", sa.Boolean, nullable=False, server_default=sa.false()
        ),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime, nullable=False, server_default=sa.func.now()
        ),
        sa.Column("ended_at", sa.DateTime, nullable=True),
    )
    op.create_index(
        "uq_investigations_project_slot",
        "investigations",
        ["project_id"],
        unique=True,
        postgresql_where=sa.text("pods_gone = false"),
        sqlite_where=sa.text("pods_gone = 0"),
    )
    op.create_table(
        "investigation_logs",
        sa.Column(
            "seq",
            sa.BigInteger().with_variant(sa.Integer, "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("line", sa.String(length=2000), nullable=False),
    )
    op.create_index(
        "ix_investigation_logs_run_seq", "investigation_logs", ["run_id", "seq"]
    )


def downgrade() -> None:
    op.drop_index("ix_investigation_logs_run_seq", table_name="investigation_logs")
    op.drop_table("investigation_logs")
    op.drop_index("uq_investigations_project_slot", table_name="investigations")
    op.drop_table("investigations")
